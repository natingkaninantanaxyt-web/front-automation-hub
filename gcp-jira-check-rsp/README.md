# gcp-jira-check-rsp

เช็คตั๋ว Jira "RSP Sync : Effective on ..." ที่ยังไม่ปิด ว่าร้านค้าไหน sync ราคาแล้วบ้าง เทียบกับ log
`FetchRetailPrice` บน GCP (`tdshop-prod` / `PosApp`) แล้ว comment สรุปกลับที่ตั๋ว — เป็นหนึ่งโมดูลใน
[Front Automation Hub](../README.md)

## Repository overview

โมดูลนี้คือไฟล์ HTML ไฟล์เดียว (`index.html`) ที่ทำงานฝั่ง client ทั้งหมด ไม่มี build system, server,
หรือ backend ใดๆ — เปิดตรงในเบราว์เซอร์ได้เลย สี/ฟอนต์/โทนพื้นฐานดึงมาจาก [`../shared/theme.css`](../shared/theme.css)

**ต่างจาก CrossFormat ตรงที่ไม่มี "engine" ทำงานอยู่ในหน้าเว็บเลย** เพราะงานจริง (query `gcloud logging read`
และเรียก Jira REST API ด้วย personal token) ต้องรันจากเครื่องผู้ใช้เท่านั้น — browser ยิง request ตรงไปที่
`jira.tdshop.io` ไม่ได้ (CORS + ต้องมี credential) และไม่มีทางรัน `gcloud` จาก JS ในเบราว์เซอร์ หน้านี้จึงทำหน้าที่แค่:

1. ให้ดาวน์โหลด [`gcp_jira_check_rsp.py`](./gcp_jira_check_rsp.py) — สคริปต์ตัวจริงที่รันการเช็ค
2. โชว์คำสั่ง terminal สำหรับรัน (`--dry-run` หรือรันจริง)
3. รับ output ที่ผู้ใช้ copy-paste กลับมา แล้ว parse+render เป็นตารางอ่านง่าย (`parseOutput()` ใน `index.html`) — ประมวลผลในเบราว์เซอร์ล้วน ไม่ส่งข้อมูลไปที่ไหน

## `gcp_jira_check_rsp.py`

สคริปต์ python เดี่ยว ไม่มี dependency นอก stdlib ทำงาน stage หลักๆ: `search_open_rsp_tickets` (JQL: ticket
label `PS_Front` + summary มีคำว่า "RSP Sync" + ยังไม่ปิด) → `parse_ticket` (ดึง barcode/store list/effective
date จาก wiki-table ใน description ด้วย regex) → `query_synced_stores` (สร้าง `gcloud logging read` filter
จาก store list + barcode + event `FetchRetailPrice`) → diff เทียบ store list กับที่เจอใน log → `set_assignee`
(assign ticket ให้เป็นคนที่ run ตาม token ที่ใช้ — **เฉพาะถ้ายังไม่มี assignee เดิม** ไม่งั้นข้ามไปเลย) →
`post_comment` (skip ถ้าผลลัพธ์เหมือน comment ล่าสุดที่มี marker `Auto RSP Sync Check` อยู่แล้ว) → status/flag
transition (ดูหัวข้อด้านล่าง)

### Status transitions + flag

อิงจาก workflow จริงที่ทีมใช้ปิดตั๋วพวกนี้ด้วยมืออยู่แล้ว (เช็คจากตั๋วเก่าที่ปิดไปแล้วอย่าง SUP-13505/13506):

- **status = Open** → transition ไป **In Progress ก่อนเสมอ** ไม่ว่า sync ครบหรือยังไม่ครบก็ตาม (ดูจุดสำคัญข้อ 2)
- **status = In Progress** (ไม่ว่าจะเพิ่ง transition มาจาก Open ในรอบนี้ หรือเป็น In Progress มาจากรอบก่อนแล้วก็ตาม)
  → เช็ค missing ซ้ำทุกรอบแล้ว **reconcile flag ให้ตรงกับสถานะจริงเสมอ**: ถ้ายัง sync ไม่ครบ ติด flag `Impediment`
  ไว้ (ติดซ้ำก็ไม่เป็นไร, idempotent) ถ้า sync ครบแล้วเอา flag ออก + transition ไป **Close** พร้อมตั้ง
  `resolution = "Won't Do"` และ `fixVersions = ["Won't Fix Release"]`

จุดสำคัญ:
1. การเช็ค flag **ไม่ได้ผูกอยู่กับจังหวะ transition Open→In Progress เท่านั้น** เพราะถ้าทำแบบนั้น ตั๋วที่
   เข้า In Progress ไปแล้วตั้งแต่รอบก่อน (เช่นเจอบั๊ก endpoint ผิดใน v1.2.0 ที่ทำให้ flag ไม่ติดจริง) จะไม่มีทาง
   ถูก flag ซ้ำได้อีกเลยในรอบต่อๆไป เพราะ status ไม่ใช่ "Open" แล้ว — เช็คทุกรอบจาก missing ปัจจุบันจึงกันปัญหานี้ได้
2. ตั๋วที่เป็น **Open แล้วพบว่า sync ครบทุกร้านแล้วตั้งแต่รอบแรกที่เช็ค** (ยังไม่เคยผ่าน In Progress มาก่อนเลย)
   ต้องเดินผ่าน Open → In Progress → Close **ในรอบเดียวกัน** ไม่ใช่ค้างอยู่ที่ Open เพราะเงื่อนไข transition
   เดิมเช็คแค่ตอน `missing` เท่านั้น (แก้ใน v1.2.3) — โค้ดจึงแยก "ทรานสิชันออกจาก Open" กับ "ตัดสินใจ flag/Close"
   เป็นสองบล็อกอิสระ ให้ตั๋วที่ผ่านบล็อกแรกมาหมาดๆ ตกไปอยู่ในเงื่อนไข In Progress ของบล็อกที่สองได้ทันที

Transition ID ไม่ได้ hardcode ไว้ — `get_transition_id` เรียก `/issue/{key}/transitions` สดทุกครั้งแล้วหาด้วย
**ชื่อ** transition ("In Progress" / "Close") เพื่อกันปัญหาถ้า workflow เปลี่ยน ID ในอนาคต ถ้าหาไม่เจอ (เช่น
workflow ถูกแก้ไปแล้ว) จะ print WARNING แล้วข้าม ไม่ทำให้ script ทั้งรอบ crash

**เรื่อง flag (`set_flag`):** field `customfield_10107` (Flagged) เซ็ตผ่าน `PUT /issue/{key}` ธรรมดาไม่ได้ —
Jira ตอบ 400 กับ `"errors": {"customfield_10107": "...not on the appropriate screen..."}` เพราะ field นี้ผูกกับ
ปุ่ม Flag บน board เท่านั้น ไม่ได้อยู่บน edit screen ไหนเลย ต้องยิงไปที่ endpoint ของปุ่มนั้นตรงๆ (ไม่มี doc
เป็นทางการ แต่ทดสอบแล้วใช้งานได้แน่นอน): `POST /rest/greenhopper/1.0/xboard/issue/flag/flag.json` body
`{"issueKeys": [key], "flag": true/false}`

**บั๊กที่เจอจากเรื่องนี้ (แก้แล้วใน v1.2.1):** `jira_request` เดิมเช็ค error จาก response แค่ key `errorMessages`
ซึ่ง Jira ตอบ error field-validation แบบข้างบนมาที่ key `errors` (คนละ key) แทน — HTTP 400 จริงแต่ `errorMessages`
เป็น `[]` (falsy) เลยไม่ raise exception ทำให้ script รายงานว่า "flagged" สำเร็จ ทั้งที่ Jira ไม่ได้เซ็ตอะไรให้เลย
แก้โดยให้ `jira_request` เช็ค **HTTP status code จริงจาก curl** (`-w "\n%{http_code}"`) แทนการเดาจาก shape ของ
response body — ครอบคลุม error ทุกแบบ ไม่ใช่แค่ที่ Jira เลือกจะใส่ใน `errorMessages`

เช็ค permission ของ token ผ่าน `/rest/api/2/mypermissions?projectKey=SUP` แล้วว่า `ASSIGN_ISSUES`,
`EDIT_ISSUES`, `TRANSITION_ISSUES`, `RESOLVE_ISSUES` ทุกตัว = true สำหรับ token ที่ทดสอบไว้ — token คนอื่นในทีม
ที่ role เดียวกันควรจะผ่านหมดเหมือนกัน แต่ถ้า role ต่างกันอาจต้องเช็คซ้ำ

**Jira credential** resolve ตามลำดับ (ดู `load_jira_creds`) เพื่อให้ทุกคนในทีมรันได้ ไม่ต้องพึ่ง Claude Code:
1. env vars `JIRA_URL` / `JIRA_PERSONAL_TOKEN`
2. config file `~/.rsp_sync_check.json` (`{"jira_url": ..., "jira_token": ...}`)
3. fallback: `~/.claude.json` (`mcpServers.mcp-atlassian.env`) — สำหรับคนที่ใช้ Claude Code อยู่แล้ว

ไม่มี token ฝังอยู่ในไฟล์นี้เลยไม่ว่ากรณีไหน ปลอดภัยที่จะ commit ขึ้น public repo เพราะ credential จริงอยู่แค่บนเครื่อง
ผู้ใช้เท่านั้น เรียก Jira ผ่าน `curl` (ไม่ใช่ Python `urllib`) เพราะ certifi bundle ของ Python ไม่มี CA ขององค์กร
ที่ใช้เซ็น cert ของ `jira.tdshop.io` แต่ macOS system trust store (ที่ `curl` ใช้) มี

## Versioning / staleness check

`SCRIPT_VERSION` ใน `gcp_jira_check_rsp.py` ต้อง**ตรงกับไฟล์ [`VERSION`](./VERSION)** เสมอ — ทุกครั้งที่แก้ logic ในสคริปต์
ต้อง bump ทั้งสองที่คู่กัน (`VERSION` เก็บแค่เลขเวอร์ชันดิบๆ ไม่มี newline พิเศษอะไร) เพราะตัวสคริปต์จะ fetch ไฟล์
`VERSION` จาก `raw.githubusercontent.com` ทุกครั้งที่ run (`check_for_updates`) มาเทียบกับ `SCRIPT_VERSION` ของตัวเอง
ถ้าไม่ตรงจะเตือนผู้ใช้ว่าไฟล์ที่โหลดไว้เก่าแล้ว ให้ไปโหลดใหม่ก่อน (ถาม y/N ว่าจะรันต่อทั้งที่เก่าไหม) — ถ้าลืม bump
`VERSION` ตอน push การเตือนนี้จะไม่ทำงาน ผู้ใช้เก่าจะไม่รู้ตัวว่าไฟล์ตัวเองล้าสมัยไปแล้ว

### หน้าเว็บ (`index.html`) เองก็ต้องเช็ค staleness เหมือนกัน

พบปัญหาซ้ำหลายรอบว่า browser cache หน้า `index.html` เก่าไว้ (เห็นได้จาก Incognito แล้วเจอเวอร์ชันใหม่ถูกต้อง แต่
หน้าปกติยังเก่าอยู่) — meta tag `Cache-Control`/`Pragma`/`Expires` ใน `<head>` ช่วยได้จำกัดมาก (browser ส่วนใหญ่ไม่แคร์
http-equiv Cache-Control ตอน navigate ปกติ) ตัวที่แก้จริงคือ **meta tag `page-version`** เทียบกับ `VERSION` สด
(fetch แบบ `{cache: 'no-store'}` กันปัญหา fetch ตัวเองก็ถูก cache ไปด้วย) ถ้าไม่ตรง = หน้านี้เป็นของเก่าที่ค้าง cache
อยู่ → auto `location.replace` ไป URL เดิมแต่ต่อ query string ใหม่ (`?_v=<version>`) หนึ่งครั้งเพื่อ force ให้ browser
fetch ทับของเก่า (กัน loop ด้วย `sessionStorage`)

**สำคัญ: ทุกครั้งที่ bump `SCRIPT_VERSION`/`VERSION` ต้อง bump `<meta name="page-version" content="...">` ใน
`index.html` ให้ตรงกันด้วย** ไม่งั้นกลไกนี้จะเข้าใจผิดว่าหน้าตัวเองเก่าอยู่ตลอด (เพราะเทียบกับเลขที่ไม่ได้อัปเดต)
แล้ว reload วนหนึ่งครั้งเปล่าๆทุกรอบที่มีการ push เวอร์ชันสคริปต์ใหม่ทั้งที่หน้าเว็บจริงๆไม่ได้เปลี่ยน

## Running / developing

- เปิด `gcp-jira-check-rsp/index.html` ตรงในเบราว์เซอร์ ไม่ต้อง install อะไร
- ทดสอบ `gcp_jira_check_rsp.py` แยกจากหน้าเว็บได้เลย: `python3 gcp_jira_check_rsp.py --dry-run` (ต้อง `gcloud auth login`
  และมี credential ตามที่ระบุไว้ข้างบนก่อน) เพิ่ม `--skip-update-check` ถ้าไม่อยากให้เช็คเวอร์ชันตอนเทส
- ถ้าแก้ logic ใน `gcp_jira_check_rsp.py` ต้อง sync ให้ตรงกับตัวต้นฉบับที่ `~/scripts/rsp_sync_check/rsp_sync_check.py`
  บนเครื่อง (ใช้รันแบบ manual/cron ได้เหมือนกัน — ชื่อไฟล์ path นี้ยังไม่ได้เปลี่ยนตาม เพราะเป็นไฟล์จริงนอก repo, ดู
  หมายเหตุท้าย PR) — สองไฟล์นี้เป็นคนละไฟล์ ไม่ได้ symlink กัน
