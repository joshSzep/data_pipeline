# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import unicode_literals

import pytest

from data_pipeline import message as dp_message
from data_pipeline.message_type import MessageType


class SharedMessageTest(object):

    @pytest.fixture
    def message(self, valid_message_data):
        return self.message_class(**valid_message_data)

    def test_rejects_unicode_topic(self, valid_message_data):
        self._assert_invalid_data(valid_message_data, topic=unicode('topic'))

    def test_rejects_empty_topic(self, valid_message_data):
        self._assert_invalid_data(valid_message_data, topic=str(''))

    def test_rejects_non_numeric_schema_id(self, valid_message_data):
        self._assert_invalid_data(valid_message_data, schema_id='123')

    def test_rejects_junk_uuid(self, valid_message_data):
        self._assert_invalid_data(valid_message_data, uuid='junk')

    def test_rejects_pii_data(self, valid_message_data):
        self._assert_invalid_data(valid_message_data, NotImplementedError, contains_pii=True)

    @pytest.mark.parametrize("invalid_upstream_pos_info", ['test', ['test']])
    def test_rejects_non_dicts_in_upstream_position_info(
        self,
        valid_message_data,
        invalid_upstream_pos_info
    ):
        self._assert_invalid_data(
            valid_message_data,
            upstream_position_info=invalid_upstream_pos_info
        )

    def test_rejects_non_kafka_position_info(self, valid_message_data):
        self._assert_invalid_data(valid_message_data, kafka_position_info=123)

    @pytest.mark.parametrize("empty_payload", [None, ""])
    def test_rejects_message_without_payload(self, valid_message_data, empty_payload):
        self._assert_invalid_data(
            valid_message_data,
            payload=empty_payload,
            payload_data=None
        )

    @pytest.mark.parametrize("empty_payload_data", [None, {}])
    def test_rejects_message_without_payload_data(
        self,
        valid_message_data,
        empty_payload_data
    ):
        self._assert_invalid_data(
            valid_message_data,
            payload=None,
            payload_data=empty_payload_data
        )

    @pytest.mark.parametrize("invalid_payload", [100, ['test'], {'data': 'foo'}])
    def test_rejects_non_bytes_payload(self, valid_message_data, invalid_payload):
        self._assert_invalid_data(
            valid_message_data,
            payload=invalid_payload,
            payload_data=None
        )

    @pytest.mark.parametrize("invalid_payload_data", [100, ['test'], bytes(10)])
    def test_rejects_non_dict_payload_data(
        self,
        valid_message_data,
        invalid_payload_data
    ):
        self._assert_invalid_data(
            valid_message_data,
            payload=None,
            payload_data=invalid_payload_data
        )

    def test_rejects_both_payload_and_payload_data(self, valid_message_data):
        self._assert_invalid_data(
            valid_message_data,
            payload=bytes(10),
            payload_data={'data': 'foo'}
        )

    def _assert_invalid_data(self, valid_data, error=ValueError, **data_overrides):
        invalid_data = self._make_message_data(valid_data, **data_overrides)
        with pytest.raises(error):
            self.message_class(**invalid_data)

    def _make_message_data(self, valid_data, **overrides):
        message_data = dict(valid_data)
        message_data.update(**overrides)
        return message_data

    def test_generates_uuid(self, message):
        assert isinstance(message.uuid, bytes) and len(message.uuid) == 16

    def test_accepts_dicts_in_upstream_position_info(self, valid_message_data):
        message_data = self._make_message_data(
            valid_message_data,
            upstream_position_info=dict(something='some_unicode')
        )
        message = self.message_class(**message_data)
        assert isinstance(message, self.message_class)

    def test_message_type(self, message):
        assert message.message_type == self.expected_message_type

    def test_dry_run(self, valid_message_data):
        payload_data = {'data': 'test'}
        message_data = self._make_message_data(
            valid_message_data,
            payload=None,
            payload_data=payload_data,
            dry_run=True
        )
        dry_run_message = self.message_class(**message_data)
        assert dry_run_message.payload == repr(payload_data)


class PayloadOnlyMessageTest(SharedMessageTest):

    @pytest.fixture(params=[(bytes(10), None), (None, {'data': 'test'})])
    def valid_message_data(self, request):
        payload, payload_data = request.param
        return {
            'topic': str('my-topic'),
            'schema_id': 123,
            'payload': payload,
            'payload_data': payload_data
        }

    def test_rejects_previous_payload(self, message):
        with pytest.raises(AttributeError):
            message.previous_payload

    def test_rejects_previous_payload_data(self, message):
        with pytest.raises(AttributeError):
            message.previous_payload_data


class TestCreateMessage(PayloadOnlyMessageTest):

    @property
    def message_class(self):
        return dp_message.CreateMessage

    @property
    def expected_message_type(self):
        return MessageType.create


class TestRefreshMessage(PayloadOnlyMessageTest):

    @property
    def message_class(self):
        return dp_message.RefreshMessage

    @property
    def expected_message_type(self):
        return MessageType.refresh


class TestDeleteMessage(PayloadOnlyMessageTest):

    @property
    def message_class(self):
        return dp_message.DeleteMessage

    @property
    def expected_message_type(self):
        return MessageType.delete


class TestUpdateMessage(SharedMessageTest):

    @property
    def message_class(self):
        return dp_message.UpdateMessage

    @property
    def expected_message_type(self):
        return MessageType.update

    @pytest.fixture(params=[
        (bytes(10), None, bytes(100), None),
        (None, {'data': 'test'}, None, {'foo': 'bar'})
    ])
    def valid_message_data(self, request):
        payload, payload_data, previous_payload, previous_payload_data = request.param
        return dict(
            topic=str('my-topic'),
            schema_id=123,
            payload=payload,
            payload_data=payload_data,
            previous_payload=previous_payload,
            previous_payload_data=previous_payload_data
        )

    @pytest.mark.parametrize("empty_previous_payload", [None, ""])
    def test_rejects_message_without_previous_payload(
        self,
        valid_message_data,
        empty_previous_payload
    ):
        self._assert_invalid_data(
            valid_message_data,
            previous_payload=empty_previous_payload,
            previous_payload_data=None
        )

    @pytest.mark.parametrize("empty_previous_payload_data", [None, {}])
    def test_rejects_message_without_previous_payload_data(
        self,
        valid_message_data,
        empty_previous_payload_data
    ):
        self._assert_invalid_data(
            valid_message_data,
            previous_payload=None,
            previous_payload_data=empty_previous_payload_data
        )

    @pytest.mark.parametrize(
        "invalid_previous_payload",
        [1, ['test'], {'foo': 'bar'}]
    )
    def test_rejects_non_bytes_previous_payload(
        self,
        valid_message_data,
        invalid_previous_payload
    ):
        self._assert_invalid_data(
            valid_message_data,
            previous_payload=invalid_previous_payload,
            previous_payload_data=None
        )

    @pytest.mark.parametrize(
        "invalid_previous_payload_data",
        [1, ['test'], bytes(10)]
    )
    def test_rejects_non_dict_previous_payload_data(
        self,
        valid_message_data,
        invalid_previous_payload_data
    ):
        self._assert_invalid_data(
            valid_message_data,
            previous_payload=None,
            previous_payload_data=invalid_previous_payload_data
        )

    def test_rejects_both_previous_payload_and_payload_data(self, valid_message_data):
        self._assert_invalid_data(
            valid_message_data,
            previous_payload=bytes(10),
            previous_payload_data={'foo': 'bar'}
        )
