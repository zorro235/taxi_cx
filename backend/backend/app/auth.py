import os,json,hmac,hashlib
from datetime import datetime,timezone
from urllib.parse import parse_qsl

def validate_init_data(init_data,bot_token,max_age=3600):
    if not bot_token or not init_data: raise ValueError('authorization not configured')
    pairs=dict(parse_qsl(init_data,keep_blank_values=True)); received=pairs.pop('hash',None)
    if not received: raise ValueError('missing hash')
    data='\n'.join(f'{k}={pairs[k]}' for k in sorted(pairs));secret=hmac.new(b'WebAppData',bot_token.encode(),hashlib.sha256).digest();expected=hmac.new(secret,data.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,received): raise ValueError('invalid hash')
    auth=int(pairs.get('auth_date','0'))
    if auth<=0 or datetime.now(timezone.utc).timestamp()-auth>max_age: raise ValueError('expired')
    user=json.loads(pairs['user'])
    if not user.get('id'): raise ValueError('invalid user')
    return user
