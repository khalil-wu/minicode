import asyncio
import json

import httpx
import pytest

from backend.config_helpers import LLMSettings
from backend.llm.base import LLMMessage, StreamEventType, StreamEvent
from backend.llm.openai_adapter import OpenAIAdapter
from backend.agent.provider_stream_error_event import provider_error_details


def completed(output=None):
    return {"type":"response.completed", "response":{"id":"resp-test", "status":"completed", "model":"gpt-6.1-sol", "output":output if output is not None else [{"type":"message","id":"msg-test","role":"assistant","status":"completed","content":[{"type":"output_text","text":"verified result","annotations":[]}]}]}}


def consume(wire, tools=None):
    async def main():
        async def respond(request):
            assert request.url.path.endswith('/responses')
            body=''.join('data: '+json.dumps(item)+'\n\n' for item in wire)+'data: [DONE]\n\n'
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=body.encode(),request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            adapter=OpenAIAdapter(LLMSettings(provider='custom',api_key='unit-fixture',base_url='https://example.test/v1',model='gpt-6.1-sol',wire_api='responses',proxy_mode='direct'),http_client=client)
            events=[event async for event in adapter.stream_chat([LLMMessage(role='user',content='Return a verified result.')],tools=tools)]
            await adapter.aclose()
            return events
    return asyncio.run(main())


@pytest.mark.parametrize('kind',['response.future.metadata','response.extension.progress','response.reasoning.private_status'])
def test_typed_unhandled_observation_keeps_terminal_authority_and_bounded_diagnostics(kind):
    wire=[{'type':kind,'sequence_number':1,'delta':'private payload never shown'},completed()]
    events=consume(wire)
    assert not [event for event in events if event.type==StreamEventType.ERROR]
    done=next(event for event in events if event.type==StreamEventType.DONE)
    assert done.finish_reason=='completed'
    assert any(row.get('event')==kind and row.get('unhandled') for row in done.raw['provider_timeline'])
    assert 'private payload never shown' not in json.dumps(done.raw)
    assert ''.join(event.content for event in events if event.type==StreamEventType.TEXT_CHUNK)=='verified result'
    assert not [event for event in events if event.type==StreamEventType.TOOL_CALL]


def test_unhandled_observation_without_terminal_never_becomes_success():
    events=consume([{'type':'response.future.metadata','sequence_number':1}])
    assert not [event for event in events if event.type==StreamEventType.DONE]
    error=next(event for event in events if event.type==StreamEventType.ERROR)
    assert error.raw['event_type']=='eof_without_terminal'


def test_untyped_observation_requires_a_valid_terminal_and_never_becomes_text():
    events=consume([{'delta':'untyped data'},completed()])
    done=next(event for event in events if event.type==StreamEventType.DONE)
    assert any(row['event']=='stream.event.untyped' and row.get('unhandled') for row in done.raw['provider_timeline'])
    assert 'untyped data' not in json.dumps(done.raw)
    assert 'untyped data' not in ''.join(event.content for event in events)
    incomplete=consume([{'delta':'untyped data'}])
    assert not [event for event in incomplete if event.type==StreamEventType.DONE]
    assert any(event.type==StreamEventType.ERROR for event in incomplete)


def test_untyped_error_is_not_mistaken_for_a_keepalive():
    events=consume([{'error':{'code':'invalid_api_key','message':'Incorrect API key provided.','type':'authentication_error'}},completed()])
    assert not [event for event in events if event.type==StreamEventType.DONE]
    error=next(event for event in events if event.type==StreamEventType.ERROR)
    assert error.raw['provider_error_code']=='invalid_api_key'


def test_actual_error_envelope_is_not_ignored_when_the_typed_event_is_new():
    events=consume([{'type':'response.future.failure','error':{'code':'invalid_api_key','message':'Incorrect API key provided.','type':'authentication_error'}},completed()])
    assert not [event for event in events if event.type==StreamEventType.DONE]
    error=next(event for event in events if event.type==StreamEventType.ERROR)
    assert error.raw['event_type']=='response.future.failure'
    assert error.raw['provider_error_code']=='invalid_api_key'


def test_completed_unsupported_executable_item_is_not_accepted_after_metadata():
    events=consume([{'type':'response.future.metadata'},completed([{'type':'computer_call','id':'call-test','call_id':'call-computer','action':{'type':'click','x':1,'y':2},'status':'completed'}])])
    assert not [event for event in events if event.type==StreamEventType.DONE]
    assert not [event for event in events if event.type==StreamEventType.TOOL_CALL]
    error=next(event for event in events if event.type==StreamEventType.ERROR)
    assert 'computer_call' in error.raw['output_item_types']


def test_failed_provider_diagnostic_preserves_the_rejected_event_name():
    event=StreamEvent(type=StreamEventType.ERROR,content='Malformed response event',raw={'provider_error_type':'protocol','error_type':'api','protocol_error_code':'missing_stream_event_type','event_type':'missing'})
    _,_,details=provider_error_details(event)
    assert details['protocol_error_code']=='missing_stream_event_type'
    assert details['provider_event_type']=='missing'
