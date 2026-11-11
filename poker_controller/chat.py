"""Normalize live table chat for the shared inbox."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import uuid


def incoming_bluffpoker_chat(event, received=None):
    """Stable identity across table subscribers; BluffPoker has no history endpoint."""
    table, player, author, body = (event.get(k) for k in ('tableId','playerId','screenName','text'))
    if not all(isinstance(value,str) and value.strip() for value in (table,player,author,body)):
        return None
    received=received or datetime.now(timezone.utc)
    posted=None
    try:
        if isinstance(event.get('at'),(int,float)) and not isinstance(event['at'],bool):
            posted=datetime.fromtimestamp(event['at']/1000,timezone.utc)
    except (ValueError,OverflowError,OSError):
        pass
    # A broadcast's server timestamp identifies it across accounts and machines.
    # Without one, retain each occurrence rather than merging repeated messages.
    identity=[table,player,body,event.get('at') if posted else str(uuid.uuid4())]
    key=hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()
    return {'table_id':table,'event_key':key,'author':author.encode('utf-16-le','replace')[:200].decode('utf-16-le','ignore'),
            'body':body.encode('utf-16-le','replace')[:4000].decode('utf-16-le','ignore'),
            'posted':posted,'received':received,'historical':False}


def incoming_chat(event, received=None):
    table=event.get("ti") or event.get("tableId")
    try: table=int(table)
    except (TypeError,ValueError): return []
    if table<=0:
        return []
    received=received or datetime.now(timezone.utc)
    historical=bool(event.get("history"))
    result=[]
    for index,raw in enumerate(event.get("chatMessage") or []):
        if not isinstance(raw,dict): continue
        author=str(raw.get("a") or "").strip()
        body=str(raw.get("m") or "").strip()
        if not author or not body or author.casefold()=="dealer":
            continue
        posted=None
        try:
            posted=datetime.fromisoformat(str(raw.get("r") or "").replace("Z","+00:00"))
            if posted.tzinfo is None: posted=posted.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        # Server timestamps/identifiers deduplicate the same broadcast seen by
        # several accounts. With legacy payloads the worker reads one bot per
        # table and gives each live occurrence its own identity.
        identity=[str(table),raw]
        if not raw.get("r") and not raw.get("i"):
            identity.append(index if historical else str(uuid.uuid4()))
        key=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        result.append({"table_id":str(table),"event_key":key,"author":author.encode("utf-16-le","replace")[:200].decode("utf-16-le","ignore"),"body":body.encode("utf-16-le","replace")[:4000].decode("utf-16-le","ignore"),
                       "posted":posted,"received":received,"historical":historical})
    return result
