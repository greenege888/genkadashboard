# Genka Deals: daily inbox routine (Gmail connector version)

Use this file when the routine reads mail through the **Gmail connector** rather than
over IMAP. Same job as `ROUTINE.md`: everything forwarded to the intake address ends up
in the Genka Deals Supabase backend, filed when you are confident, queued for review when
you are not.

Use `ingest.py` for every Supabase write. Never delete emails or rows.

## What the connector can and cannot do

- **Can:** search threads, read a message in full with `get_message`
  (use `messageFormat: PLAIN_TEXT`), see attachment **filenames and sizes**, reply,
  forward, trash.
- **Cannot:** download attachment contents. Filenames are visible, the bytes are not.

So documents are handled one of two ways, in this order of preference:

1. **From the Drive intake folder.** A Zap copies every Gmail attachment into a Drive
   folder called **Genka Intake** (see `docs/zap-gmail-attachments.md`; Gmail's own
   "save to Drive" button on an attachment does the same thing by hand). So:
   - search that folder with the Drive connector for a file whose name contains the
     attachment filename, created around the time of the email. If the name is prefixed
     with the Gmail message id, match on that instead, which is exact.
   - download it, which returns base64; write that to a scratch file, e.g. `b64.txt`
   - `python3 ingest.py docs <project_id>` and skip it if the same name and size is
     already filed
   - `python3 ingest.py upload-b64 <project_id> <category> "<original file name>" @b64.txt`

   This is the fully automatic path.
2. **Flagged for the human.** Otherwise, name every attachment in the activity summary
   and queue an `inbox_items` row of type `document` so it appears on the app's Inbox tab.
   Ege uploads it from the project's Data room in a couple of taps.

Never claim a document was filed when only its name was recorded.

## Steps

1. **Load context.**
   - `python3 ingest.py projects` gives every project and deal, for matching.
   - `python3 ingest.py seen 300` gives the message ids already processed. Skip anything
     already listed; this is what stops double filing.

2. **Find new mail.** Search the intake mailbox for threads from the last 14 days. Read
   each candidate with `get_message` using `PLAIN_TEXT`. Work through them one at a time.

3. **Decide, per message.** Match on project names, site names, SPV names, counterparty
   names, capacity figures and countries. Forwarded chains: judge by the underlying
   content, not the forwarding header.

   **A. Confident match to an existing project.**
   - Log the email:
     `python3 ingest.py insert activities '{"project_id":"<id>","activity_type":"email","activity_date":"YYYY-MM-DD","summary":"<who, what it says, what it changes; name any attachments and whether they were filed>"}'`
     Use the email's own date. Meeting invitations or recaps: `"activity_type":"meeting"`.
   - Documents: follow the two paths above. When uploading, classify into exactly one of
     teaser, im, financial_model, grid, land, permits, eia, corporate, nda, loi, spa, other.
     Before uploading, run `python3 ingest.py docs <project_id>` and skip anything already
     there with the same name and size.
   - Clear risk stated (permit refusal, grid constraint, litigation, counterparty distress):
     `python3 ingest.py insert risks '{"project_id":"<id>","title":"...","detail":"...","severity":"medium|high|blocker","status":"open"}'`
   - Obvious next step for the active deal, only if the current next_action is empty or
     clearly superseded:
     `python3 ingest.py update deals <deal_id> '{"next_action":"...","next_action_due":"YYYY-MM-DD"}'`
   - Then: `python3 ingest.py mark "gmail:<messageId>" "<subject>" filed`

   **B. Not confident, or a new opportunity.** Queue it:
   ```
   python3 ingest.py insert inbox_items '{"message_id":"gmail:<messageId>","received_at":"<ISO timestamp>","from_addr":"<sender>","subject":"<subject>","summary":"<2-3 sentences>","suggested_type":"activity|document|risk|new_project","suggested_project_id":"<uuid or null>","payload":{"project":{...},"deal":{...},"risk":{...},"next_action":{...},"docs":[{"file_name":"...","category":"teaser"}]},"status":"pending"}'
   ```
   Include only the payload parts that apply. For a teaser of an unknown project use
   `"suggested_type":"new_project"` and fill `payload.project` from the teaser content you
   can read. List attachment filenames under `payload.docs` even when you could not upload
   them, so the human knows what to attach.
   Then: `python3 ingest.py mark "gmail:<messageId>" "<subject>" review`

   **C. Noise** (newsletters, spam, out-of-office, pure logistics):
   `python3 ingest.py mark "gmail:<messageId>" "<subject>" skipped`

4. **Report.** N filed and to which projects, N queued, N skipped, which documents were
   actually uploaded versus only flagged, and anything urgent (deadlines, blockers).
   Under 10 lines.

## Judgment rules

- File directly only when the project match is unambiguous. One wrong-project filing is
  worse than ten review items.
- Never invent numbers. Prices, capacities and dates come from the email only.
- Emails in Turkish, Italian, Spanish or Romanian are normal: summarize in English.
- Prefix every id with `gmail:` when calling `mark`, so connector ids never collide with
  ids written by the IMAP version of this routine.
- Do not trash, reply to, or forward anything. This routine only reads.
- If a command errors, stop and report it rather than improvising.
