"""Expose server betting bounds and strictly validate manual actions in minor units."""
from .state import turn_key


def options(state,hero,mapper=None):
    key=mapper.turn_key(state,hero) if mapper else turn_key(state,hero)
    seat=state['s'][hero];funds=state.get('f',{})
    stack=max(0,int(seat.get('c',0)));bet=int(seat.get('b',0))
    maximum=min(int(funds.get('mx',stack)),stack)
    minimum=min(int(funds.get('mn',0)),maximum)
    legal={1:'fold',2:'check',3:'call',8:'bet',9:'raise'}
    return {'turn_key':key,'actions':[legal[a] for a in state.get('aa',[]) if a in legal] if key else [],
        'call_minor':min(int(funds.get('c',0)),stack),'hero_bet_minor':bet,
        'min_target_minor':bet+minimum,'max_target_minor':bet+maximum,
        'pot_minor':int(state.get('d',{}).get('p',0)),
        'step_minor':max(1,int(state.get('bbv',100)))}


def action_to_play(command,state,hero,mapper=None):
    o=options(state,hero,mapper)
    if not o['turn_key'] or command['TurnKey']!=o['turn_key']:
        raise ValueError('The turn changed. Refresh and choose an action for the current turn.')
    action=command['Action']
    if action not in o['actions']:raise ValueError('That action is no longer legal.')
    code={'fold':1,'check':2,'call':3,'bet':8,'raise':9}[action]
    if action in ('fold','check'):return code,0
    if action=='call':return code,o['call_minor']
    target=command.get('TargetMinor')
    if type(target) is not int or not o['min_target_minor']<=target<=o['max_target_minor']:
        raise ValueError('Bet/raise total is outside the current table limits.')
    funds=target-o['hero_bet_minor']
    if funds<=0:raise ValueError('Bet/raise must add chips.')
    return code,funds
