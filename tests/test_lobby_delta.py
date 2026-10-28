from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from poker_controller.client import EvenBetClient


@pytest.mark.asyncio
async def test_lobby_partial_rows_preserve_game_stakes_and_names():
    connection=SimpleNamespace(listeners=[],request=AsyncMock(side_effect=[
        {'tables':[{'i':42,'g':72,'p':85,'bb':200,'n':'Test','np':1}]},
        {'tables':[{'i':42,'np':3}]}, {'tables':[]}]))
    client=EvenBetClient(connection=connection)
    await client.list_tables()
    rows=await client.list_tables()
    assert rows==[{'i':42,'g':72,'p':85,'bb':200,'n':'Test','np':3}]
    assert await client.list_tables()==[]
    assert client.tables[42]['bb']==200
    client._on_message({'t':'GoToTable','info':{'i':42,'np':4}})
    assert client.tables[42]['g']==72
    assert client.tables[42]['np']==4


@pytest.mark.asyncio
async def test_existing_seat_uses_entry_index_from_player_tables():
    connection=SimpleNamespace(listeners=[],request=AsyncMock(side_effect=[
        {'tables':[{'i':42}],'playerEntryIdxs':[3]}, {}, {}, {}, {}]))
    client=EvenBetClient(connection=connection)
    client.player_id=73582719
    await client.refresh_player_tables()
    assert client.table_select_indices[42] == 3
    client._on_message({'t':'TableState','tableId':42,'seats':[
        {'pi':123,'ei':0},{'pi':73582719,'ei':1}]})
    await client.player_command(42,10)
    await client.return_from_away(42)
    await client.play(42,2)
    await client.leave(42)
    assert [call.kwargs['playerEntryIdx'] for call in connection.request.await_args_list[1:]] == [1,1,1,1]
