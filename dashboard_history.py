"""Read-only, text-only export of locally retained agent conversations."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import json, sqlite3, re, hashlib
ROOT=Path(__file__).resolve().parent
STAMP=re.compile(r'^\[(\d{4}-\d{2}-\d{2})\s+(?:[A-Za-z]+\s+)?(\d{2}:\d{2})(?::\d{2})?[^\]]*\]\s*')
INTERNAL=('[Session started:', '[Local recovery record]', '[Background scope update', '[System:', 'Core memory loaded.', '[Alex asked you to use', '[Background task')
NOTE='Locally retained text, not a complete Telegram export. System prompts, tool results, hidden reasoning and media files are excluded. Assistant timestamps may be associated user-turn times, not actual delivery times. Older messages may no longer be retained.'


def text_only(content):
    if isinstance(content,str):return content
    if not isinstance(content,list):return ''
    if any(isinstance(b,dict) and b.get('type')=='tool_result' for b in content):return ''
    text='\n'.join(b.get('text','') for b in content if isinstance(b,dict) and b.get('type')=='text')
    if not text and any(isinstance(b,dict) and b.get('type') in ('image','image_url','input_image') for b in content):
        return '[Image attachment; media not included in this export]'
    return text


def history(agent,chat,date_from='',date_to='',search=''):
    if agent not in ('cato','tacitus','callimachus'):raise ValueError('Unknown agent')
    for value in (date_from,date_to):
        if value:datetime.strptime(value,'%Y-%m-%d')
    if date_from and date_to and date_from>date_to:raise ValueError('Start date must be before end date')
    if len(search)>300:raise ValueError('Search is too long')
    folder=ROOT/agent;rows=[];main=folder/f'conversation_{chat}.json'
    archive=folder/'task_state/conversation_recall.sqlite3'
    if archive.exists():
        db=sqlite3.connect('file:'+archive.as_posix()+'?mode=ro',uri=True,timeout=3);db.row_factory=sqlite3.Row
        try:
            for r in db.execute('SELECT id,speaker,said_at,date_kind,text,source FROM messages WHERE chat=? ORDER BY id',(str(chat),)):
                # Background model transcripts and memory seeds are not personal chat history.
                source=r['source'].replace('\\','/').lower()
                if '/background/' in source:continue
                if source!=str(main).replace('\\','/').lower() and '/inbox.sqlite3#job=' not in source:continue
                rows.append(dict(role=r['speaker'],time=r['said_at'],time_kind=r['date_kind'],text=r['text'],source='local archive'))
        finally:db.close()
    if main.exists():
        try:data=json.loads(main.read_text(encoding='utf-8'))
        except ValueError:raise ValueError('Conversation is being saved; try again shortly')
        associated=''
        if isinstance(data,list):
            for m in data[2:]:
                role=m.get('role')
                if role not in ('user','assistant'):continue
                text=text_only(m.get('content')).strip()
                match=STAMP.match(text)
                if role=='user':associated=match[1]+'T'+match[2] if match else ''
                rows.append(dict(role=role,time=associated,time_kind='user turn timestamp' if role=='user' else 'associated user turn; reply time unknown',text=text,source='retained conversation'))
    unique=[];seen=set()
    for row in rows:
        raw=row['text'].strip()
        if not raw or raw.startswith(INTERNAL):continue
        body=STAMP.sub('',raw).strip()
        if body.startswith(INTERNAL):continue
        body=re.split(r'\n\n\[(?:Silent checkpoint|Alex wrote|Background work|Available images)',body,maxsplit=1)[0].strip()
        if not body:continue
        # Normalize timezone-bearing archive timestamps to local minute for duplicate matching.
        stamp=row['time']
        if stamp:
            try:
                moment=datetime.fromisoformat(stamp)
                if moment.tzinfo:moment=moment.astimezone(ZoneInfo('UTC'))
                stamp=moment.strftime('%Y-%m-%dT%H:%M')
            except ValueError:pass
        key=(row['role'],stamp[:16],body)
        if key in seen:continue
        seen.add(key)
        if date_from and (not stamp or stamp[:10]<date_from):continue
        if date_to and (not stamp or stamp[:10]>date_to):continue
        if search and search.casefold() not in body.casefold():continue
        unique.append(dict(role=row['role'],time=stamp,time_kind=row['time_kind'],text=body,source=row['source']))
    unique.sort(key=lambda r:r['time'] or '')
    return {'agent':agent,'note':NOTE,'count':len(unique),'messages':unique}


def export(data,format):
    if format=='json':return json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8'),'application/json'
    if format!='md':raise ValueError('Choose Markdown or JSON')
    lines=['# '+data['agent'].title()+' — locally retained conversation history','',data['note'],'']
    for row in data['messages']:
        speaker='Alex' if row['role']=='user' else data['agent'].title()
        lines.extend(['## '+speaker+' · '+(row['time'] or 'Time unknown'),row['time_kind'],'',row['text'],''])
    return '\n'.join(lines).encode('utf-8'),'text/markdown; charset=utf-8'
