# -*- coding: utf-8 -*-
# Copyright 2016 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import absolute_import
from __future__ import unicode_literals

import copy
import datetime
import random
import time
from uuid import uuid4

import mock
import pytest
from kafka import create_message
from yelp_kafka.producer import YelpKafkaSimpleProducer

from data_pipeline.base_consumer import BaseConsumer
from data_pipeline.base_consumer import ConsumerTopicState
from data_pipeline.base_consumer import MultipleClusterTypeError
from data_pipeline.base_consumer import TopicFilter
from data_pipeline.base_consumer import TopicNotFoundInRegionError
from data_pipeline.config import get_config
from data_pipeline.consumer_source import FixedSchemas
from data_pipeline.consumer_source import FixedTopics
from data_pipeline.consumer_source import NewTopicOnlyInDataTarget
from data_pipeline.consumer_source import NewTopicOnlyInSource
from data_pipeline.consumer_source import NewTopicsOnlyInFixedNamespaces
from data_pipeline.consumer_source import TopicInDataTarget
from data_pipeline.consumer_source import TopicInSource
from data_pipeline.consumer_source import TopicsInFixedNamespaces
from data_pipeline.envelope import Envelope
from data_pipeline.expected_frequency import ExpectedFrequency
from data_pipeline.message import UpdateMessage
from data_pipeline.producer import Producer
from data_pipeline.schematizer_clientlib.models.data_source_type_enum \
    import DataSourceTypeEnum
from data_pipeline.schematizer_clientlib.schematizer import get_schematizer
from tests.helpers.mock_utils import attach_spy_on_func


TIMEOUT = 3
""" TIMEOUT is used for all 'get_messages' calls in these tests. It's
essential that this value is large enough for the background workers
to have a chance to retrieve the messages, but otherwise as small
as possible as this has a direct impact on time it takes to execute
the tests.

Unfortunately these tests can flake if the consumers happen to take
too long to retrieve/decode the messages from kafka and miss the timeout
window and there is currently no mechanism to know if a consumer has
attempted and failed to retrieve a message we expect it to retrieve, vs
just taking longer than expected.

TODO(DATAPIPE-249|joshszep): Make data_pipeline clientlib Consumer tests
faster and flake-proof
"""


@pytest.mark.usefixtures("containers")
class FakeScribeKafka(object):

    def setup(self, containers):
        self.kafka_client = containers.get_kafka_connection()
        self.cluster_config = get_config().cluster_config
        self.producer = YelpKafkaSimpleProducer(
            client=self.kafka_client,
            cluster_config=self.cluster_config
        )

    def clog_publish(self, containers, logname, message, count):
        self.setup(containers)
        assert count > 0
        scribe_topic = self.get_scribe_kafka_topic_from_logname(logname)
        for _ in range(count):
            self.producer.send_messages(
                scribe_topic,
                self.prepare_message(message)
            )

    def prepare_message(self, message):
        return create_message(Envelope().pack(message)).value

    def get_scribe_kafka_topic_from_logname(self, log_topic):
        return str('scribe_test.devc_test.{0}'.format(log_topic))


@pytest.mark.usefixtures("configure_teams")
class BaseConsumerTest(object):

    @pytest.yield_fixture
    def producer(self, team_name):
        with Producer(
            producer_name='producer_1',
            team_name=team_name,
            expected_frequency_seconds=ExpectedFrequency.constantly,
            use_work_pool=False
        ) as producer:
            yield producer

    @pytest.fixture
    def publish_messages(self, producer):
        def _publish_messages(message, count):
            assert count > 0
            for _ in range(count):
                producer.publish(message)
            producer.flush()
        return _publish_messages

    @pytest.fixture
    def publish_log_messages(self, containers):
        def _publish_messages(topic, message, count):
            FakeScribeKafka().clog_publish(
                containers,
                topic,
                message,
                count
            )
        return _publish_messages

    @pytest.fixture(scope="module")
    def topic(self, registered_schema):
        return str(registered_schema.topic.name)

    @pytest.fixture(scope="module")
    def log_topic(self, registered_log_schema):
        return str(registered_log_schema.topic.name)

    @pytest.fixture(scope="module")
    def partitions(self):
        return [0]

    @pytest.fixture(scope="module", autouse=True)
    def ensure_topic_exist(self, containers, topic, log_topic):
        containers.create_kafka_topic(topic)
        containers.create_kafka_topic(
            FakeScribeKafka().get_scribe_kafka_topic_from_logname(log_topic)
        )

    def test_skip_commit_offset_if_offset_unchanged(
            self,
            publish_messages,
            message,
            consumer_instance
    ):
        asserter = ConsumerAsserter(
            consumer=consumer_instance,
            expected_message=message
        )
        with consumer_instance as consumer:
            publish_messages(message, 4)

            with attach_spy_on_func(
                consumer.kafka_client,
                'send_offset_commit_request'
            ) as func_spy:
                msgs_r1 = consumer.get_messages(
                    count=2,
                    blocking=True,
                    timeout=TIMEOUT
                )
                asserter.assert_messages(msgs_r1, 2)

                consumer.commit_messages(msgs_r1)
                assert func_spy.call_count == 1

                func_spy.reset_mock()

                # call_count does not increase
                # when no new msgs are commited
                consumer.commit_messages(msgs_r1)
                assert func_spy.call_count == 0

                # assert that next call to get_message should
                # get message from next offset
                msgs_r2 = consumer.get_messages(
                    count=2,
                    blocking=True,
                    timeout=TIMEOUT
                )
                asserter.assert_messages(msgs_r2, 2)

    def test_call_kafka_commit_offsets_when_offset_change(
            self,
            publish_messages,
            message,
            consumer_instance
    ):
        asserter = ConsumerAsserter(
            consumer=consumer_instance,
            expected_message=message
        )
        with consumer_instance as consumer:
            publish_messages(message, 4)

            with attach_spy_on_func(
                consumer.kafka_client,
                'send_offset_commit_request'
            ) as func_spy:
                msgs_r1 = consumer.get_messages(
                    count=3,
                    blocking=True,
                    timeout=TIMEOUT
                )
                asserter.assert_messages(msgs_r1, 3)

                consumer.commit_messages(msgs_r1)
                assert func_spy.call_count == 1

                func_spy.reset_mock()

                # call_count increases
                # when offset is different from last commited offset
                consumer.commit_message(msgs_r1[0])
                assert func_spy.call_count == 1

                func_spy.reset_mock()

                consumer.commit_message(msgs_r1[2])
                assert func_spy.call_count == 1

                # assert that next call to get_message should
                # get message from next offset
                msgs_r2 = consumer.get_messages(
                    count=1,
                    blocking=True,
                    timeout=TIMEOUT
                )
                assert len(msgs_r2) == 1
                asserter.assert_messages(msgs_r2, 1)

    def test_offset_cache_reset_on_topic_reset(
            self,
            publish_messages,
            message,
            consumer_instance
    ):
        asserter = ConsumerAsserter(
            consumer=consumer_instance,
            expected_message=message
        )
        with consumer_instance as consumer:
            publish_messages(message, 4)
            with attach_spy_on_func(
                consumer.kafka_client,
                'send_offset_commit_request'
            ) as func_spy:
                msgs = consumer.get_messages(
                    count=4,
                    blocking=True,
                    timeout=TIMEOUT
                )
                assert len(msgs) == 4
                asserter.assert_messages(msgs, 4)

                consumer.commit_messages(msgs)
                assert func_spy.call_count == 1
                topic_map = {topic: None for topic in consumer.topic_to_partition_map}

                with mock.patch.object(
                    consumer,
                    '_get_topics_in_region_from_topic_name',
                    side_effect=[[x] for x in topic_map.keys()]
                ):
                    consumer.reset_topics(topic_to_consumer_topic_state_map=topic_map)

                func_spy.reset_mock()

                # on commiting messages with same offset
                # send_offset_commit_request should get called
                # because cache is reset on consumer.reset_topics
                consumer.commit_messages(msgs)
                assert func_spy.call_count == 1

    def test_get_message_none(self, consumer_instance, topic, partitions):
        with consumer_instance as consumer:
            _messsage = consumer.get_message(blocking=True, timeout=TIMEOUT)
            assert _messsage is None
            assert consumer.topic_to_partition_map[topic] == partitions

    def test_basic_iteration(self, consumer_instance, publish_messages, message):
        with consumer_instance as consumer:
            publish_messages(message, count=1)
            asserter = ConsumerAsserter(
                consumer=consumer,
                expected_message=message
            )
            for _message in consumer:
                asserter.assert_messages([_message], expected_count=1)
                break

    @pytest.fixture
    def update_message(self, payload, registered_schema):
        return UpdateMessage(
            schema_id=registered_schema.schema_id,
            payload=payload,
            previous_payload=payload
        )

    def test_get_update_message(
        self,
        consumer_instance,
        publish_messages,
        update_message
    ):
        with consumer_instance as consumer:
            publish_messages(update_message, count=1)
            asserter = ConsumerAsserter(
                consumer=consumer,
                expected_message=update_message
            )
            messages = consumer.get_messages(
                count=1,
                blocking=True,
                timeout=TIMEOUT
            )
            asserter.assert_messages(messages, expected_count=1)

    def test_get_message(self, consumer_instance, publish_messages, message):
        with consumer_instance as consumer:
            publish_messages(message, count=1)
            asserter = ConsumerAsserter(
                consumer=consumer,
                expected_message=message
            )
            _message = consumer.get_message(blocking=True, timeout=TIMEOUT)
            asserter.assert_messages([_message], expected_count=1)

    def test_get_messages(self, consumer_instance, publish_messages, message):
        with consumer_instance as consumer:
            publish_messages(message, count=2)
            asserter = ConsumerAsserter(
                consumer=consumer,
                expected_message=message
            )
            messages = consumer.get_messages(
                count=2,
                blocking=True,
                timeout=TIMEOUT
            )
            asserter.assert_messages(messages, expected_count=2)

    def test_get_messages_then_reset(
        self,
        consumer_instance,
        publish_messages,
        message
    ):
        with consumer_instance as consumer:
            publish_messages(message, count=2)
            asserter = ConsumerAsserter(
                consumer=consumer,
                expected_message=message
            )

            # Get messages so that the topic_to_consumer_topic_state_map will
            # have a ConsumerTopicState for the topic.  Getting more messages
            # than necessary is to verify only two published messages are consumed
            messages = consumer.get_messages(
                count=10,
                blocking=True,
                timeout=TIMEOUT
            )
            asserter.assert_messages(messages, expected_count=2)

            # Set the offset to one previous so we can use reset_topics to
            # receive the same two messages again
            topic_map = {}
            for message in messages:
                topic_map[message.topic] = ConsumerTopicState(
                    partition_offset_map={
                        message.kafka_position_info.partition:
                            message.kafka_position_info.offset - 1
                    },
                    last_seen_schema_id=None
                )
            with mock.patch.object(
                consumer,
                '_get_topics_in_region_from_topic_name',
                side_effect=[[x] for x in topic_map.keys()]
            ):
                consumer.reset_topics(topic_to_consumer_topic_state_map=topic_map)

            # Verify that we do get the same two messages again
            messages = consumer.get_messages(
                count=10,
                blocking=True,
                timeout=TIMEOUT
            )
            asserter.assert_messages(messages, expected_count=2)

    def test_get_log_message(
        self,
        log_consumer_instance,
        publish_log_messages,
        log_message,
        log_topic
    ):
        with mock.patch(
            'yelp_kafka.discovery.get_region_cluster',
            return_value=get_config().cluster_config
        ):
            with log_consumer_instance as consumer:
                publish_log_messages(log_topic, log_message, count=1)
                asserter = ConsumerAsserter(
                    consumer=consumer,
                    expected_message=log_message
                )
                _message = consumer.get_message(blocking=True, timeout=TIMEOUT)
                asserter.assert_messages([_message], expected_count=1)

    def test_handle_log_and_non_log_topics_fails(
        self,
        topic,
        log_topic,
        consumer_init_kwargs
    ):
        with pytest.raises(MultipleClusterTypeError):
            BaseConsumer(
                topic_to_consumer_topic_state_map={topic: None, log_topic: None},
                auto_offset_reset='largest',
                **consumer_init_kwargs
            )

    @pytest.mark.parametrize("cluster_type, discovery_method", [
        ('scribe', 'yelp_kafka.discovery.get_region_logs_stream'),
        ('datapipe', 'yelp_kafka.discovery.search_topic')
    ])
    def test_no_topics_in_cluster_name(
        self,
        cluster_type,
        discovery_method,
        topic,
        consumer_init_kwargs
    ):
        consumer = BaseConsumer(
            topic_to_consumer_topic_state_map={topic: None},
            auto_offset_reset='largest',
            **consumer_init_kwargs
        )
        consumer.cluster_type = cluster_type
        with mock.patch(discovery_method) as mock_discovery_method:
            mock_discovery_method.return_value = []
            with pytest.raises(TopicNotFoundInRegionError):
                consumer._get_topics_in_region_from_topic_name(topic)

    def test_base_consumer_with_cluster_name(
        self,
        topic,
        consumer_init_kwargs
    ):
        cluster_name = 'uswest2-devc'
        with mock.patch(
            'yelp_kafka.discovery.get_kafka_cluster'
        ) as mock_get_kafka_cluster:
            consumer = BaseConsumer(
                topic_to_consumer_topic_state_map={topic: None},
                auto_offset_reset='largest',
                cluster_name=cluster_name,
                **consumer_init_kwargs
            )
            consumer._region_cluster_config
            mock_get_kafka_cluster.assert_called_once_with(
                client_id=consumer.client_name,
                cluster_name=cluster_name,
                cluster_type=consumer.cluster_type
            )

    def test_base_consumer_without_cluster_name(
        self,
        topic,
        consumer_init_kwargs
    ):
        with mock.patch(
            'yelp_kafka.discovery.get_kafka_cluster'
        ) as mock_get_kafka_cluster, mock.patch(
            'kafka_utils.util.config.ClusterConfig.__init__',
            return_value=None
        ) as mock_cluster_config_init:
            consumer = BaseConsumer(
                topic_to_consumer_topic_state_map={topic: None},
                auto_offset_reset='largest',
                **consumer_init_kwargs
            )
            consumer._region_cluster_config
            assert mock_get_kafka_cluster.call_count == 0
            config = get_config()
            mock_cluster_config_init.assert_called_once_with(
                type='standard',
                name='data_pipeline',
                broker_list=config.kafka_broker_list,
                zookeeper=config.kafka_zookeeper
            )


class BaseConsumerSourceBaseTest(object):

    @pytest.fixture
    def consumer_group_name(self):
        return 'test_consumer_{}'.format(random.random())

    @pytest.fixture
    def pre_rebalance_callback(self):
        return mock.Mock()

    @pytest.fixture
    def post_rebalance_callback(self):
        return mock.Mock()

    @pytest.fixture(params=[False, True])
    def force_payload_decode(self, request):
        return request.param

    @pytest.yield_fixture
    def producer(self, team_name):
        with Producer(
            producer_name='producer_1',
            team_name=team_name,
            expected_frequency_seconds=ExpectedFrequency.constantly,
            use_work_pool=False
        ) as producer:
            yield producer

    @pytest.fixture
    def publish_messages(self, producer):
        def _publish_messages(message, count):
            assert count > 0
            for _ in range(count):
                producer.publish(message)
            producer.flush()
        return _publish_messages


class ConsumerAsserter(object):
    """ Helper class to encapsulate the common assertions in the consumer tests
    """

    def __init__(self, consumer, expected_message):
        self.consumer = consumer
        self.expected_message = expected_message
        self.expected_topic = expected_message.topic
        self.expected_schema_id = expected_message.schema_id

    def get_and_assert_messages(self, count, expected_msg_count, blocking=True):
        with self.consumer.ensure_committed(
            self.consumer.get_messages(
                count=count,
                blocking=blocking,
                timeout=TIMEOUT
            )
        ) as messages:
            assert len(messages) == expected_msg_count
            self.assert_messages(messages)
        return messages

    def assert_messages(self, actual_messages, expected_count):
        assert isinstance(actual_messages, list)
        assert len(actual_messages) == expected_count
        for actual_message in actual_messages:
            self.assert_equal_message(actual_message, self.expected_message)

    def assert_equal_message(self, actual_message, expected_message):
        assert actual_message.message_type == expected_message.message_type
        assert actual_message.payload == expected_message.payload
        assert actual_message.schema_id == expected_message.schema_id
        assert actual_message.topic == expected_message.topic
        assert actual_message.payload_data == expected_message.payload_data

        if isinstance(expected_message, UpdateMessage):
            assert actual_message.previous_payload == \
                expected_message.previous_payload
            assert actual_message.previous_payload_data == \
                expected_message.previous_payload_data


@pytest.mark.usefixtures("configure_teams")
class RefreshNewTopicsTest(object):

    @pytest.fixture
    def yelp_namespace(self):
        return 'yelp_{0}'.format(random.random())

    @pytest.fixture
    def biz_src(self):
        return 'biz_{0}'.format(random.random())

    @pytest.fixture
    def biz_schema(self, yelp_namespace, biz_src, containers):
        return self._register_schema(yelp_namespace, biz_src, containers)

    @pytest.fixture
    def usr_src(self):
        return 'user_{0}'.format(random.random())

    @pytest.fixture
    def usr_schema(self, yelp_namespace, usr_src, containers):
        return self._register_schema(yelp_namespace, usr_src, containers)

    @pytest.fixture
    def aux_namespace(self):
        return 'aux_{0}'.format(random.random())

    @pytest.fixture
    def cta_src(self):
        return 'cta_{0}'.format(random.random())

    @pytest.fixture(autouse=True)
    def cta_schema(self, aux_namespace, cta_src, containers):
        return self._register_schema(aux_namespace, cta_src, containers)

    @pytest.fixture
    def pre_rebalance_callback(self):
        return mock.Mock()

    @pytest.fixture
    def post_rebalance_callback(self):
        return mock.Mock()

    def _register_schema(self, namespace, source, containers):
        avro_schema = {
            'type': 'record',
            'name': source,
            'namespace': namespace,
            'doc': 'test',
            'fields': [{'type': 'int', 'doc': 'test', 'name': 'id'}]
        }
        reg_schema = get_schematizer().register_schema_from_schema_json(
            namespace=namespace,
            source=source,
            schema_json=avro_schema,
            source_owner_email='bam+test@yelp.com',
            contains_pii=False
        )
        containers.create_kafka_topic(str(reg_schema.topic.name))
        return reg_schema

    @pytest.fixture(scope='class')
    def namespace(self):
        return 'test_namespace_{}'.format(uuid4())

    @pytest.fixture(scope='class')
    def test_schema(self, containers, namespace):
        return self._register_schema(namespace, 'test_src', containers)

    @pytest.fixture(scope='class')
    def topic(self, containers, test_schema):
        topic_name = str(test_schema.topic.name)
        containers.create_kafka_topic(topic_name)
        return topic_name

    @pytest.yield_fixture
    def consumer(self, consumer_instance):
        with consumer_instance as consumer:
            yield consumer

    def test_no_newer_topics(self, consumer, yelp_namespace, biz_schema):
        expected = self._get_expected_value(
            original_states=consumer.topic_to_partition_map
        )

        new_topics = consumer.refresh_new_topics(TopicFilter(
            namespace_name=yelp_namespace,
            created_after=self._increment_seconds(biz_schema.created_at, seconds=1)
        ))
        assert new_topics == []
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map=expected
        )

    def test_refresh_newer_topics_in_yelp_namespace(
        self,
        consumer,
        yelp_namespace,
        biz_schema,
        usr_schema
    ):
        biz_topic = biz_schema.topic
        usr_topic = usr_schema.topic
        expected = self._get_expected_value(
            original_states=consumer.topic_to_partition_map,
            new_states={biz_topic.name: [0], usr_topic.name: [0]}
        )

        new_topics = consumer.refresh_new_topics(TopicFilter(
            namespace_name=yelp_namespace,
            created_after=self._increment_seconds(
                min(biz_schema.created_at, usr_schema.created_at),
                seconds=-1
            )
        ))

        assert new_topics == [biz_topic, usr_topic]
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map=expected
        )

    def test_already_tailed_topic_partition_map_remains_after_refresh(
        self,
        consumer,
        test_schema,
    ):
        self._publish_then_consume_message(consumer, test_schema)

        topic = test_schema.topic
        expected = self._get_expected_value(
            original_states=consumer.topic_to_partition_map
        )

        new_topics = consumer.refresh_new_topics(TopicFilter(
            source_name=topic.source.name,
            created_after=self._increment_seconds(topic.created_at, seconds=-1)
        ))

        assert topic.topic_id not in [new_topic.topic_id for new_topic in new_topics]
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map=expected
        )

    def _publish_then_consume_message(self, consumer, avro_schema):
        with Producer(
            'test_producer',
            team_name='bam',
            expected_frequency_seconds=ExpectedFrequency.constantly,
            monitoring_enabled=False
        ) as producer:
            message = UpdateMessage(
                schema_id=avro_schema.schema_id,
                payload_data={'id': 2},
                previous_payload_data={'id': 1}
            )
            producer.publish(message)
            producer.flush()

        consumer.get_messages(1, blocking=True, timeout=TIMEOUT)

    def test_refresh_with_custom_filter(
        self,
        consumer,
        yelp_namespace,
        biz_schema,
        usr_schema
    ):
        biz_topic = biz_schema.topic
        expected = self._get_expected_value(
            original_states=consumer.topic_to_partition_map,
            new_states={biz_topic.name: [0]}
        )

        new_topics = consumer.refresh_new_topics(TopicFilter(
            namespace_name=yelp_namespace,
            created_after=self._increment_seconds(
                min(biz_schema.created_at, usr_schema.created_at),
                seconds=-1
            ),
            filter_func=lambda topics: [biz_topic]
        ))

        assert new_topics == [biz_topic]
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map=expected
        )

    def test_with_bad_namespace(self, consumer):
        actual = consumer.refresh_new_topics(TopicFilter(
            namespace_name='bad.namespace',
            created_after=0
        ))
        assert actual == []

    def test_with_bad_source(self, consumer, yelp_namespace):
        actual = consumer.refresh_new_topics(TopicFilter(
            namespace_name=yelp_namespace,
            source_name='bad.source',
            created_after=0
        ))
        assert actual == []

    def test_with_pre_topic_refresh_callback(
        self,
        consumer,
        yelp_namespace,
        biz_schema
    ):
        mock_pre_topic_refresh_callback_handler = mock.Mock()
        old_topic_names = consumer.topic_to_partition_map.keys()
        new_topics = consumer.refresh_new_topics(
            TopicFilter(
                namespace_name=yelp_namespace,
                created_after=self._increment_seconds(
                    biz_schema.created_at,
                    seconds=-1
                )
            ),
            pre_topic_refresh_callback=mock_pre_topic_refresh_callback_handler
        )
        biz_topic = biz_schema.topic
        assert new_topics == [biz_topic]
        mock_pre_topic_refresh_callback_handler.assert_called_once_with(
            old_topic_names,
            [biz_topic.name]
        )

    def _get_utc_timestamp(self, dt):
        utc_zero = datetime.datetime(1970, 1, 1, tzinfo=dt.tzinfo)
        return int((dt - utc_zero).total_seconds())

    def _increment_seconds(self, dt, seconds):
        return self._get_utc_timestamp(dt + datetime.timedelta(seconds=seconds))

    def _get_expected_value(self, original_states, new_states=None):
        expected = copy.deepcopy(original_states)
        expected.update(new_states or {})
        return expected

    def _assert_equal_partition_map(self, actual_map, expected_map):
        assert set(actual_map.keys()) == set(expected_map.keys())
        for topic, actual_partitions in actual_map.iteritems():
            expected_partitions = expected_map[topic]
            if expected_partitions is None:
                assert actual_partitions is None
                continue
            assert set(actual_partitions) == set(expected_partitions)


@pytest.mark.usefixtures("configure_teams")
class RefreshTopicsTestBase(object):

    @pytest.yield_fixture
    def consumer(self, consumer_instance):
        with consumer_instance as consumer:
            yield consumer

    @pytest.fixture(scope='module')
    def _register_schema(self, containers, schematizer_client):
        def register_func(namespace, source, avro_schema=None):
            avro_schema = avro_schema or {
                'type': 'record',
                'name': source,
                'doc': 'test',
                'namespace': namespace,
                'fields': [{'type': 'int', 'doc': 'test', 'name': 'id'}]
            }
            new_schema = schematizer_client.register_schema_from_schema_json(
                namespace=namespace,
                source=source,
                schema_json=avro_schema,
                source_owner_email='bam+test@yelp.com',
                contains_pii=False
            )
            containers.create_kafka_topic(str(new_schema.topic.name))
            return new_schema
        return register_func

    def random_name(self, prefix=None):
        suffix = random.random()
        return '{}_{}'.format(prefix, suffix) if prefix else '{}'.format(suffix)

    @pytest.fixture
    def foo_namespace(self):
        return self.random_name('foo_ns')

    @pytest.fixture
    def foo_src(self):
        return self.random_name('foo_src')

    @pytest.fixture
    def foo_schema(self, foo_namespace, foo_src, _register_schema):
        return _register_schema(foo_namespace, foo_src)

    @pytest.fixture
    def foo_topic(self, foo_schema):
        return foo_schema.topic.name

    @property
    def random_data_target_name(self):
        return 'name_{}'.format(random.random())

    @property
    def target_type(self):
        return 'redshift'

    @property
    def destination(self):
        return 'dw.redshift.destination'

    @pytest.fixture
    def data_target(self, schematizer_client):
        return schematizer_client.create_data_target(
            name=self.random_data_target_name,
            target_type=self.target_type,
            destination=self.destination
        )

    @pytest.fixture
    def consumer_group(self, data_target, schematizer_client):
        return schematizer_client.create_consumer_group(
            group_name=self.random_name('test_group'),
            data_target_id=data_target.data_target_id
        )

    @pytest.fixture
    def data_source(self, foo_schema, consumer_group, schematizer_client):
        return schematizer_client.create_consumer_group_data_source(
            consumer_group_id=consumer_group.consumer_group_id,
            data_source_type=DataSourceTypeEnum.Source,
            data_source_id=foo_schema.topic.source.source_id
        )

    @pytest.fixture(scope='class')
    def namespace(self):
        return 'test_namespace_{}'.format(uuid4())

    @pytest.fixture(scope='class')
    def test_schema(self, _register_schema, namespace):
        return _register_schema(namespace, 'test_src')

    @pytest.fixture(scope='class')
    def topic(self, test_schema):
        return str(test_schema.topic.name)

    @pytest.fixture(scope='class')
    def partitions(self):
        return [0]

    @pytest.fixture
    def schema_with_bad_topic(self, schematizer_client, foo_namespace, foo_src):
        avro_schema = {
            'type': 'record',
            'name': foo_src,
            'doc': 'test',
            'namespace': foo_namespace,
            'fields': [{'type': 'int', 'doc': 'test', 'name': 'id'}]
        }
        new_schema = schematizer_client.register_schema_from_schema_json(
            namespace=foo_namespace,
            source=foo_src,
            schema_json=avro_schema,
            source_owner_email='bam+test@yelp.com',
            contains_pii=False
        )
        return new_schema

    def _publish_then_consume_message(self, consumer, avro_schema):
        with Producer(
            'test_producer',
            team_name='bam',
            expected_frequency_seconds=ExpectedFrequency.constantly,
            monitoring_enabled=False
        ) as producer:
            message = UpdateMessage(
                schema_id=avro_schema.schema_id,
                payload_data={'id': 2},
                previous_payload_data={'id': 1}
            )
            producer.publish(message)
            producer.flush()

        consumer.get_messages(1, blocking=True, timeout=TIMEOUT)

    def _assert_equal_partition_map(self, actual_map, expected_map):
        assert set(actual_map.keys()) == set(expected_map.keys())
        for topic, actual_partitions in actual_map.iteritems():
            expected_partitions = expected_map[topic]
            if expected_partitions is None:
                assert actual_partitions is None
                continue
            assert set(actual_partitions) == set(expected_partitions)


class RefreshFixedTopicTests(RefreshTopicsTestBase):

    def test_get_topics(
        self,
        consumer,
        consumer_source,
        expected_topics,
        topic,
        partitions
    ):
        expected_map = {topic: partitions}
        expected_map.update({topic: partitions for topic in expected_topics})

        actual = consumer.refresh_topics(consumer_source)

        assert set(actual) == set(expected_topics)
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map=expected_map
        )

    def test_get_topics_multiple_times(
        self,
        consumer,
        consumer_source,
        expected_topics,
        topic,
        partitions
    ):
        expected_map = {topic: partitions}
        expected_map.update({topic: partitions for topic in expected_topics})

        actual = consumer.refresh_topics(consumer_source)
        assert set(actual) == set(expected_topics)
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map=expected_map
        )

        actual = consumer.refresh_topics(consumer_source)
        assert actual == []
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map=expected_map
        )

    def test_bad_topic(self, consumer, bad_consumer_source, topic, partitions):
        actual = consumer.refresh_topics(bad_consumer_source)

        assert actual == []
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map={topic: partitions}
        )


class TopicsInFixedNamespacesAutoRefreshSetupMixin(object):

    @pytest.fixture
    def consumer_source(self, registered_auto_refresh_schema):
        return TopicsInFixedNamespaces(
            registered_auto_refresh_schema.topic.source.namespace.name
        )


class TopicInSourceAutoRefreshSetupMixin(object):

    @pytest.fixture
    def consumer_source(self, registered_auto_refresh_schema):
        return TopicInSource(
            namespace_name=registered_auto_refresh_schema.
            topic.source.namespace.name,
            source_name=registered_auto_refresh_schema.topic.source.name
        )


class SingleTopicSetupMixin(RefreshFixedTopicTests):

    @pytest.fixture
    def expected_topics(self, foo_topic):
        return [foo_topic]

    @pytest.fixture
    def consumer_source(self, foo_topic):
        return FixedTopics(foo_topic)

    @pytest.fixture
    def bad_consumer_source(self):
        return FixedTopics('bad_topic')


class MultiTopicsSetupMixin(RefreshFixedTopicTests):

    @pytest.fixture
    def bar_schema(self, foo_namespace, foo_src, _register_schema):
        new_schema = {
            'type': 'record',
            'name': foo_src,
            'doc': 'test',
            'namespace': foo_namespace,
            'fields': [{'type': 'string', 'doc': 'test', 'name': 'memo'}]
        }
        return _register_schema(foo_namespace, foo_src, new_schema)

    @pytest.fixture
    def bar_topic(self, bar_schema):
        return bar_schema.topic.name

    @pytest.fixture
    def expected_topics(self, foo_topic, bar_topic):
        return [foo_topic, bar_topic]

    @pytest.fixture
    def consumer_source(self, foo_topic, bar_topic):
        return FixedTopics(foo_topic, bar_topic)

    @pytest.fixture
    def bad_consumer_source(self):
        return FixedTopics('bad_topic_1', 'bad_topic_2')


class FixedSchemasSetupMixin(RefreshFixedTopicTests):

    @pytest.fixture
    def consumer_source(self, foo_schema, foo_schema2):
        return FixedSchemas(
            foo_schema.schema_id,
            foo_schema2.schema_id,
        )

    @pytest.fixture
    def foo_schema(
        self,
        foo_namespace,
        foo_src,
        _register_schema
    ):
        return _register_schema(foo_namespace, foo_src)

    @pytest.fixture
    def foo_schema2(
        self,
        foo_namespace,
        foo_src,
        _register_schema,
    ):
        avro_schema2 = {
            'type': 'record',
            'name': foo_src,
            'doc': 'test',
            'namespace': foo_namespace,
            'fields': [{'type': 'int', 'name': 'id1', 'doc': 'test'}]
        }
        return _register_schema(foo_namespace, foo_src, avro_schema2)

    @pytest.fixture
    def expected_topics(self, foo_schema, foo_schema2):
        return {
            foo_schema.topic.name,
            foo_schema2.topic.name
        }

    @pytest.fixture
    def bad_consumer_source(self, schema_with_bad_topic):
        return FixedSchemas(schema_with_bad_topic.schema_id)


class RefreshDynamicTopicTests(RefreshTopicsTestBase):

    def test_no_topics_in_consumer_source(
        self,
        consumer,
        consumer_source,
        topic,
        partitions
    ):
        actual = consumer.refresh_topics(consumer_source)
        assert actual == []
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map={topic: partitions}
        )

    def test_pick_up_new_topic(
        self,
        consumer,
        consumer_source,
        foo_topic,
        data_source,
        test_schema,
        topic,
        partitions
    ):
        assert consumer.topic_to_partition_map == {topic: partitions}

        self._publish_then_consume_message(consumer, test_schema)
        partition_list = consumer.topic_to_partition_map[topic]
        assert partition_list == partitions

        actual = consumer.refresh_topics(consumer_source)

        assert actual == [foo_topic]
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map={topic: partitions, foo_topic: partitions}
        )

    def test_not_pick_up_new_topic_in_diff_source(
        self,
        consumer,
        consumer_source,
        foo_topic,
        data_source,
        topic,
        partitions,
        _register_schema
    ):
        actual = consumer.refresh_topics(consumer_source)
        assert actual == [foo_topic]
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map={topic: partitions, foo_topic: partitions}
        )

        # force topic is created at least 1 second after last topic query.
        time.sleep(1)
        new_schema = {
            'type': 'record',
            'name': 'src_two',
            'doc': 'test',
            'namespace': 'namespace_two',
            'fields': [{'type': 'bytes', 'doc': 'test', 'name': 'md5'}]
        }
        _register_schema('namespace_two', 'src_two', new_schema)

        actual = consumer.refresh_topics(consumer_source)
        assert actual == []
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map={topic: partitions, foo_topic: partitions}
        )

    def test_bad_consumer_source(
        self,
        consumer,
        bad_consumer_source,
        topic,
        partitions
    ):
        actual = consumer.refresh_topics(bad_consumer_source)

        assert actual == []
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map={topic: partitions}
        )

    def test_consumer_source_has_bad_topic(
        self,
        consumer,
        consumer_source_with_bad_topic,
        topic,
        partitions
    ):
        actual = consumer.refresh_topics(consumer_source_with_bad_topic)

        assert actual == []
        self._assert_equal_partition_map(
            actual_map=consumer.topic_to_partition_map,
            expected_map={topic: partitions}
        )


class TopicsInFixedNamespacesSetupMixin(RefreshDynamicTopicTests):

    @pytest.fixture(params=[
        TopicsInFixedNamespaces,
        NewTopicsOnlyInFixedNamespaces
    ])
    def consumer_source_cls(self, request):
        return request.param

    @pytest.fixture
    def consumer_source(self, consumer_source_cls, foo_namespace):
        return consumer_source_cls(foo_namespace)

    @pytest.fixture
    def bad_consumer_source(self, consumer_source_cls):
        return consumer_source_cls('bad_namespace')

    @pytest.fixture
    def consumer_source_with_bad_topic(
        self,
        consumer_source_cls,
        schema_with_bad_topic
    ):
        return consumer_source_cls(
            schema_with_bad_topic.topic.source.namespace.name
        )


class TopicInSourceSetupMixin(RefreshDynamicTopicTests):

    @pytest.fixture(params=[TopicInSource, NewTopicOnlyInSource])
    def consumer_source_cls(self, request):
        return request.param

    @pytest.fixture
    def consumer_source(self, consumer_source_cls, foo_namespace, foo_src):
        return consumer_source_cls(
            namespace_name=foo_namespace,
            source_name=foo_src
        )

    @pytest.fixture
    def bad_consumer_source(self, consumer_source_cls):
        return consumer_source_cls(
            namespace_name='bad_namespace',
            source_name='bad_source'
        )

    @pytest.fixture
    def consumer_source_with_bad_topic(
        self,
        consumer_source_cls,
        schema_with_bad_topic
    ):
        return consumer_source_cls(
            namespace_name=schema_with_bad_topic.topic.source.namespace.name,
            source_name=schema_with_bad_topic.topic.source.name
        )


class TopicInDataTargetSetupMixin(RefreshDynamicTopicTests):

    @pytest.fixture(params=[TopicInDataTarget, NewTopicOnlyInDataTarget])
    def consumer_source_cls(self, request):
        return request.param

    @pytest.fixture
    def consumer_source(self, consumer_source_cls, data_target):
        return consumer_source_cls(data_target_id=data_target.data_target_id)

    @pytest.fixture
    def bad_consumer_source(self, consumer_source_cls, schematizer_client):
        data_target = schematizer_client.create_data_target(
            name=self.random_data_target_name,
            target_type='bad target type',
            destination='bad destination'
        )
        return consumer_source_cls(data_target_id=data_target.data_target_id)

    @pytest.fixture
    def consumer_source_with_bad_topic(
        self,
        consumer_source_cls,
        schema_with_bad_topic,
        consumer_group,
        schematizer_client
    ):
        data_target = schematizer_client.create_data_target(
            name=self.random_data_target_name,
            target_type='some target type',
            destination='some destination'
        )
        schematizer_client.create_consumer_group_data_source(
            consumer_group_id=consumer_group.consumer_group_id,
            data_source_type=DataSourceTypeEnum.Source,
            data_source_id=schema_with_bad_topic.topic.source.source_id
        )
        return consumer_source_cls(data_target.data_target_id)
