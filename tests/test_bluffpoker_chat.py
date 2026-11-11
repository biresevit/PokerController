import asyncio
import time
from datetime import timezone
from unittest.mock import AsyncMock

import pytest

from poker_controller.bluffpoker import BluffPokerClient, BluffPokerSettings, TableSocket
from poker_controller.chat import incoming_bluffpoker_chat

TABLE='c64012de-493e-4a35-a760-1923f1369b70'
EVENT=dict(type='chatMessage',tableId=TABLE,playerId='human-id',screenName='Human',text='gl all',at=1794200000000)


def client():
    c=BluffPokerClient(BluffPokerSettings(username='bot',hub_url='https://hub.test',api_key='test',origin='https://test'))
    c.player_id='bot-id'
    c.states[TABLE]={'s':[{'i':c.player_id,'n':'bot','f':0}], 'bp':{'chat_enabled':True}}
    s=TableSocket(c,TABLE,TABLE);c.sockets[TABLE]=s
    return c,s


def test_live_chat_uuid_timestamp_and_cross_account_deduplication():
    one=incoming_bluffpoker_chat(EVENT)
    assert one['table_id']==TABLE and one['body']=='gl all' and not one['historical']
    assert one['posted'].tzinfo==timezone.utc
    assert one['event_key']==incoming_bluffpoker_chat(dict(EVENT))['event_key']
    assert one['event_key']!=incoming_bluffpoker_chat({**EVENT,'at':EVENT['at']+1})['event_key']
    assert incoming_bluffpoker_chat({**EVENT,'text':None}) is None
    assert incoming_bluffpoker_chat({**EVENT,'at':1e100})['posted'] is None


async def test_send_waits_for_own_echo_and_records_broadcast_without_reply():
    c,s=client()
    async def send(payload):
        assert payload=={'type':'chat','tableId':TABLE,'text':'gl all'}
        s.handle(EVENT)  # another player cannot confirm this write
        assert not s.chat_pending[1].done()
        s.handle({**EVENT,'playerId':c.player_id,'screenName':'bot'})
        return True
    s.send=AsyncMock(side_effect=send)
    try:
        await c.send_chat(TABLE,' gl all ')
        assert s.send.await_count==1 and s.chat_pending is None
        assert [c.chat_events.get_nowait()['author'] for _ in range(2)]==['Human','bot']
        assert c.chat_events.empty()
        await c.read_chat(TABLE)
        assert s.send.await_count==1  # live-only: no invented history request or echo reply
    finally:await c.close()


@pytest.mark.parametrize('code',['CHAT_DISABLED','CHAT_NOT_SEATED','CHAT_BANNED','CHAT_RATE_LIMIT'])
async def test_server_rejections_are_definite_failures_and_never_retried(code):
    c,s=client()
    async def send(payload):
        s.handle({'type':'error','code':code});return True
    s.send=AsyncMock(side_effect=send)
    try:
        with pytest.raises(ValueError):await c.send_chat(TABLE,'hello')
        assert s.send.await_count==1 and s.chat_pending is None
        assert c.connection.failure is None  # chat errors do not interrupt poker
    finally:await c.close()


async def test_missing_confirmation_is_uncertain_without_resend():
    c,s=client();s.send=AsyncMock(return_value=True)
    try:
        with pytest.raises(TimeoutError):await asyncio.wait_for(c.send_chat(TABLE,'hello'),.02)
        assert s.send.await_count==1 and s.chat_pending is None
    finally:await c.close()


async def test_validation_chat_disabled_and_observer_do_not_write():
    c,s=client();s.send=AsyncMock(return_value=True)
    try:
        for text in ('',' ','x'*301,'x\x00x','😀'*151):
            with pytest.raises(ValueError):await c.send_chat(TABLE,text)
        c.states[TABLE]['bp']['chat_enabled']=False
        with pytest.raises(ValueError,match='disabled'):await c.send_chat(TABLE,'hello')
        c.states[TABLE]['s']=[]
        with pytest.raises(ValueError,match='seated'):await c.send_chat(TABLE,'hello')
        s.send.assert_not_awaited()
    finally:await c.close()


async def test_rate_limit_is_shared_across_tables_and_socket_close_unblocks_sender():
    c,s=client();times=[]
    other=TABLE+'-other';second=TableSocket(c,other,other)
    c.states[other]=c.states[TABLE];c.sockets[other]=second
    def echo(sock):
        async def send(payload):
            times.append(time.monotonic())
            sock.handle({**EVENT,'tableId':sock.wire,'playerId':c.player_id,'screenName':'bot','text':payload['text']})
            return True
        return AsyncMock(side_effect=send)
    s.send=echo(s);second.send=echo(second)
    try:
        await asyncio.gather(c.send_chat(TABLE,'hello'),c.send_chat(other,'hi'))
        assert times[1]-times[0]>=.8
        c.next_chat_at=0;s.send=AsyncMock(return_value=True)
        task=asyncio.create_task(c.send_chat(TABLE,'bye'))
        await asyncio.sleep(.01);await s.close()
        with pytest.raises(ConnectionError):await task
    finally:await c.close()


async def test_wrong_table_broadcast_and_buffer_overflow():
    c,s=client()
    try:
        s.handle({**EVENT,'tableId':'other'})
        assert c.chat_events.empty()
        c.chat_events=asyncio.Queue(maxsize=1)
        s.handle(EVENT);s.handle({**EVENT,'text':'next'})
        assert c.chat_overflow and c.chat_events.qsize()==1
    finally:await c.close()
