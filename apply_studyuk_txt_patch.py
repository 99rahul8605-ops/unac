"""Safely add StudyUK source selection to current bot.py without replacing the file."""
from pathlib import Path
import ast
import shutil
from datetime import datetime

path = Path(__file__).resolve().parent / 'bot.py'
s = path.read_text(encoding='utf-8')
start_mark = '    # AUTO_TXT_FLOW_V1\n'
end_mark = '    if low in ("/start", "/search"):'
new_block = '''    # STUDYUK_TXT_SOURCE_MENU_V1
    if low == "/txt":
        from studyuk_txt import STATES as studyuk_states
        studyuk_states.pop(OWNER, None)
        menus.pop(OWNER, None)
        await event.respond(
            "📄 TXT Generator\\n\\nChoose source:",
            buttons=[
                [Button.inline("📚 HRVertex (Old)", b"txtsource:hrvertex")],
                [Button.inline("🎓 StudyUK (New)", b"txtsource:studyuk")],
            ],
        )
        return

    from studyuk_txt import handle_message as studyuk_txt_message
    if await studyuk_txt_message(event, OWNER):
        return

'''
cb_block = '''    # STUDYUK_TXT_CALLBACK_ROUTE_V1
    from studyuk_txt import handle_callback as studyuk_txt_callback
    if await studyuk_txt_callback(event, OWNER):
        return
    source_choice = (event.data or b"").decode("utf-8", "replace")
    if source_choice == "txtsource:studyuk":
        await event.answer()
        menus.pop(OWNER, None)
        from studyuk_txt import start as studyuk_txt_start
        await studyuk_txt_start(event, OWNER)
        return
    if source_choice == "txtsource:hrvertex":
        await event.answer()
        from studyuk_txt import STATES as studyuk_states
        studyuk_states.pop(OWNER, None)
        menus[OWNER] = {"step": "category", "page": 0, "mode": "txt"}
        await event.respond(
            "📄 HRVertex TXT Generator\\n\\n"
            "Category/search keyword bhejo.\\n"
            "Example: CA / CA Intermediate"
        )
        return

'''
if '# STUDYUK_TXT_SOURCE_MENU_V1' in s:
    print('StudyUK /txt hooks already present; no changes applied.')
    raise SystemExit(0)
if s.count(start_mark) != 1 or s.count(end_mark) != 1:
    raise SystemExit('Bot.py layout differs: no patch made. Inspect the /txt block manually.')
head, tail = s.split(start_mark, 1)
if end_mark not in tail:
    raise SystemExit('Missing /start anchor: no patch made.')
_, remainder = tail.split(end_mark, 1)
s = head + new_block + end_mark + remainder
callback_anchor = '@tg.on(events.CallbackQuery)'
if s.count(callback_anchor) != 1:
    raise SystemExit('Callback layout differs: no patch made.')
idx = s.index(callback_anchor)
find_text = '    state = menus.get(OWNER)'
pos = s.find(find_text, idx)
if pos == -1:
    raise SystemExit('Callback state anchor missing: no patch made.')
s = s[:pos] + cb_block + s[pos:]
ast.parse(s)
backup = path.with_name('bot.py.pre_studyuk_txt_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.bak')
shutil.copy2(path, backup)
path.write_text(s, encoding='utf-8')
print('StudyUK /txt enabled. Backup:', backup)
