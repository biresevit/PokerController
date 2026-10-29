import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from poker_controller.chat import incoming_chat
from poker_controller.client import EvenBetClient,Settings


def test_incoming_chat_uses_both_table_ids_and_ignores_dealer_and_initial_null():
    assert incoming_chat({"chatMessage":None})==[]
    event={"tableId":42,"chatMessage":[{"a":"Dealer","m":"New hand"},{"a":"Human","m":"Hello","r":"2026-10-29 08:30:00","i":123}]}
    rows=incoming_chat(event)
    assert len(rows)==1 and rows[0]["table_id"]=="42" and rows[0]["author"]=="Human"
    assert rows[0]["posted"].tzinfo==timezone.utc
    assert rows[0]["event_key"]==incoming_chat({**event,"ti":42})[0]["event_key"]
    minimal={"ti":42,"chatMessage":[{"a":"Human","m":"Hello"}]}
    assert incoming_chat(minimal)[0]["event_key"]!=incoming_chat(minimal)[0]["event_key"]


class Connection:
    def __init__(self): self.listeners=[];self.sequence=100;self.sent=[];self.echo=True
    async def send(self,topic,**fields):
        self.sequence+=1;self.sent.append((topic,fields))
        if self.echo and not fields.get('history'):
            for listener in self.listeners:
                listener({"t":"Chat","ti":fields['tableId'],"chatMessage":fields['chatMessage']})
        return self.sequence


def client():
    connection=Connection()
    c=EvenBetClient(Settings(username='monica'),connection)
    c.player_id=7;c.states[42]={"s":[{"i":7,"n":"monica","f":0}]}
    c.table_entry_indices[42]=3
    return c,connection


async def test_chat_send_uses_hopper_wire_format_and_waits_for_echo():
    c,connection=client()
    await c.send_chat(42,' Hello ')
    topic,wire=connection.sent[0]
    assert topic=='Chat' and wire['tableId']==42 and wire['playerEntryIdx']==3
    assert wire['chatMessage'][0]['m']=='Hello' and wire['chatMessage'][0]['a']=='monica'
    assert not c.chat_waiters
    assert c.chat_events.get_nowait()['body']=='Hello'


async def test_no_seat_cannot_send_and_uncertain_send_is_not_retried():
    c,connection=client()
    c.states.clear()
    with pytest.raises(ValueError): await c.send_chat(42,'Hello')
    assert connection.sent==[]
    c.states[42]={"s":[{"i":7,"n":"monica","f":0}]};connection.echo=False
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(c.send_chat(42,'Hello'),.02)
    assert len(connection.sent)==1 and not c.chat_waiters


async def test_history_is_not_used_to_confirm_a_send():
    c,connection=client()
    await c.read_chat(42)
    c._on_message({'t':'Chat','srcMsgId':101,'ti':42,'chatMessage':[{'a':'Human','m':'Old message'}]})
    assert c.chat_events.get_nowait()['historical']
