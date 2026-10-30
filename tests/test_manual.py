import copy
import pytest
from poker_controller.manual import options,action_to_play
from poker_controller.bot import Bot
from poker_controller.client import EvenBetClient


def test_manual_sizes_are_raise_to_and_validate_server_bounds(state):
    o=options(state,2)
    command={'TurnKey':o['turn_key'],'Action':'raise','TargetMinor':o['min_target_minor']}
    code,funds=action_to_play(command,state,2)
    assert code==9 and funds==o['min_target_minor']-state['s'][2]['b']
    command['TargetMinor']=o['max_target_minor']+1
    with pytest.raises(ValueError,match='limits'):action_to_play(command,state,2)


def test_manual_stale_turn_or_illegal_check_is_rejected(state):
    key=options(state,2)['turn_key']
    with pytest.raises(ValueError,match='legal'):action_to_play({'TurnKey':key,'Action':'check'},state,2)
    with pytest.raises(ValueError,match='turn changed'):action_to_play({'TurnKey':'0'*64,'Action':'fold'},state,2)


def test_manual_short_all_in_and_pot_limit_use_server_maximum(state):
    state['f'].update(mn=10000,mx=500)
    o=options(state,2)
    assert o['min_target_minor']==o['max_target_minor']==state['s'][2]['b']+500
    assert action_to_play({'TurnKey':o['turn_key'],'Action':'raise','TargetMinor':o['max_target_minor']},state,2)==(9,500)


async def test_manual_mode_does_not_call_decisionmaking(state):
    class Decisions:
        async def decide(self,frame):raise AssertionError('Automatic decision called in manual mode')
    class Control:
        async def offer(self,key):return True,None
    c=EvenBetClient();c.player_id=73582719;c.states[state['ti']]=state
    bot=Bot(c,Decisions(),state['ti'],control=Control())
    await bot.process(state)
    assert not bot.submitted


async def test_enabling_manual_during_pending_decision_blocks_auto_submission(state):
    class Decisions:
        async def decide(self,frame):return {'Action':1}
    class Control:
        async def offer(self,key):return False,None
        async def claim_auto(self,key):return False
    c=EvenBetClient();c.player_id=73582719;c.states[state['ti']]=state
    async def play(*args):raise AssertionError('Automatic action submitted after manual takeover')
    c.play=play
    await Bot(c,Decisions(),state['ti'],control=Control()).process(state)


async def test_manual_action_never_retries_ambiguous_network_send(state):
    from schedule_controller.manual_worker import ManualBridge
    class Repo:
        def manual_offer(self,*args):return True,command
        def claim_table_action(self,*args):return True
        def finish_manual_action(self,*args):results.append(args)
    results=[];played=[]
    key=options(state,2)['turn_key']
    command={'ActionId':'action','RequestId':'request','TurnKey':key,'Action':'call'}
    c=EvenBetClient();c.player_id=73582719;c.states[state['ti']]=state
    async def play(*args):played.append(args);raise TimeoutError()
    c.play=play
    bot=Bot(c,object(),state['ti'],control=ManualBridge(Repo(),1,state['ti']))
    bot.mapper.convert(state,{})
    command['TurnKey']=bot.mapper.turn_key(state,2)
    await bot.process(state);await bot.process(copy.deepcopy(state))
    assert len(played)==1 and results[-1][1]=='uncertain'
