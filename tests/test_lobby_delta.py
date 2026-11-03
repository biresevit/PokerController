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
async def test_tournament_inventory_merges_partial_rows_and_unchanged_ids():
    sng={'i':42,'n':'Sandstorm','tt':71,'g':72,'p':85,'s':71,'b':100,'np':2,'mx':6}
    mtt={'i':43,'n':'Daily MTT','tt':83,'g':72,'p':85,'s':71,'b':200,'np':5,'mx':100}
    connection=SimpleNamespace(listeners=[],request=AsyncMock(side_effect=[
        {'tournaments':[sng,mtt]},
        {'tournaments':[{'i':42,'np':4},{'i':43}]},
        {'tournaments':[{'i':42,'s':82}]},
        {'tournaments':[]}]))
    client=EvenBetClient(connection=connection)
    await client.list_tournaments()
    rows=await client.list_tournaments()
    assert rows==[{**sng,'np':4},mtt]
    rows[0]['n']='A caller must not change cached metadata'
    assert (await client.list_tournaments())==[{**sng,'np':4,'s':82}]
    assert 43 not in client.tournaments # Removed from the server's inventory.
    assert await client.list_tournaments()==[] and not client.tournaments


@pytest.mark.asyncio
async def test_failed_tournament_response_does_not_clear_confirmed_metadata():
    connection=SimpleNamespace(listeners=[],request=AsyncMock(side_effect=[
        {'tournaments':[{'i':42,'n':'Sandstorm'}]}, {}]))
    client=EvenBetClient(connection=connection)
    await client.list_tournaments()
    with pytest.raises(ValueError,match='tournament list'):
        await client.list_tournaments()
    assert client.tournaments=={42:{'i':42,'n':'Sandstorm'}}


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
