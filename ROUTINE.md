# Genka Deals: daily inbox routine

You are processing the Genka deal inbox. Everything forwarded to the dedicated
address must end up in the Genka Deals Supabase backend: filed automatically
when you are confident, or queued for human review when you are not.
Work from the folder containing `ingest.py`. Use only `ingest.py` to touch
Supabase. Never delete emails, rows, or files.

## Steps

1. **Fetch.** Run `python3 ingest.py fetch`. Each new email becomes a folder
   `inbox_work/<key>/` with `meta.json`, `body.txt`, and `attachments/`.
   If nothing is new, report "inbox empty" and stop.

2. **Load context.** Run `python3 ingest.py projects` once. This gives you all
   projects (id, name, country, tech, MWp) and deals (id, stage, status,
   next_action). Use it to match emails to projects. Match on project names,
   site names, SPV names, counterparty names, capacity figures, and countries.
   Forwarded chains: judge by the underlying content, not the forwarding header.

3. **Process each item folder, one at a time.** Read `meta.json` and
   `body.txt`, then review every attachment as described in the
   Attachment handling section below. Decide:

   **A. Confident match to an existing project.** File it directly:
   - Always log the email:
     `python3 ingest.py insert activities '{"project_id":"<id>","activity_type":"email","activity_date":"YYYY-MM-DD","summary":"<from whom, what it says, what it changes; 2-4 sentences; name the documents that came with it>"}'`
     Use the email's own date. Meeting invitations or recaps: use
     `"activity_type":"meeting"` and the meeting date.
   - File each relevant attachment per the Attachment handling section.
   - If the email or a document states a clear risk (permit refusal, grid
     constraint, litigation, counterparty distress):
     `python3 ingest.py insert risks '{"project_id":"<id>","title":"...","detail":"...","severity":"medium|high|blocker","status":"open"}'`
   - If the email creates an obvious next step for the ACTIVE deal on that
     project (for example "please sign the NDA by Friday", or an upcoming
     meeting), update it only if the current next_action is empty or clearly
     superseded:
     `python3 ingest.py update deals <deal_id> '{"next_action":"...","next_action_due":"YYYY-MM-DD"}'`
   - Then: `python3 ingest.py done inbox_work/<key> filed`

   **B. Not confident, or a new opportunity.** Queue it for review:
   - Write `inbox_work/<key>/payload.json`:
     ```json
     {
       "summary": "2-3 sentences on what this email is and why it matters",
       "suggested_type": "activity | document | risk | new_project",
       "suggested_project_id": "<uuid or null>",
       "doc_categories": {"<attachment filename>": "teaser"},
       "payload": {
         "project": {"name": "...", "country": "...", "technology": "solar|bess|hybrid|wind|other",
                      "capacity_mwp": 0, "development_status": "permitting", "location": null,
                      "grid_status": null, "land_status": null, "revenue_route": "tbd", "notes": "..."},
         "deal": {"deal_type": "buy_side", "asking_price": null, "currency": "EUR"},
         "risk": {"title": "...", "detail": "...", "severity": "medium"},
         "next_action": {"text": "...", "due": "YYYY-MM-DD"}
       }
     }
     ```
     Include only the payload parts that apply. Fill `doc_categories` for every
     attachment worth keeping, using the same category rules as below; for a
     teaser of an unknown project, use `"suggested_type": "new_project"` and
     fill `payload.project` (and `payload.deal` if a price is quoted) from the
     teaser's actual content, since you have read it.
   - Run `python3 ingest.py review inbox_work/<key>` (this also uploads the
     attachments and attaches them to the item).
   - Then: `python3 ingest.py done inbox_work/<key> review`

   **C. Noise** (newsletters, spam, out-of-office, pure logistics with no
   deal content): `python3 ingest.py done inbox_work/<key> skipped`

4. **Report.** Finish with a short summary: N filed (which projects, which
   documents), N queued for review, N skipped, plus anything urgent you
   noticed (deadlines, blockers). Keep it under 10 lines.

## Attachment handling

For every file in `attachments/`, in this order:

1. **Open and read it before deciding anything.** Read PDFs directly. For
   .docx or .xlsx, extract text quickly (for example with a short python
   snippet using zipfile on the XML inside); if extraction is impractical,
   judge from the filename, sheet or heading names, and the email context.
2. **Decide relevance.** File documents that belong in a deal data room.
   Skip logos, email signature images, .ics calendar files, tracking pixels,
   and inline images; mention skipped files only if genuinely ambiguous.
3. **Classify into exactly one category:**
   - `teaser` - short marketing deck or one-pager introducing an opportunity
   - `im` - long-form information memorandum
   - `financial_model` - spreadsheets with cash flows, IRR, P50/P90 inputs
   - `grid` - connection agreements, ATR/STMG letters, TSO/DSO correspondence
     (e-distribuzione, Terna, Transelectrica, REE, Elia and similar)
   - `land` - leases, superficies, easements, land registry extracts
   - `permits` - building or construction permits, single authorizations,
     zoning decisions
   - `eia` - environmental assessments and screenings (EIA, ESIA, VIA)
   - `corporate` - SPV registry extracts, bylaws, org charts, shareholder docs
   - `nda` - confidentiality agreements, clean or signed
   - `loi` - NBOs, LOIs, offer letters
   - `spa` - SPA drafts, mark-ups, escrow documents
   - `other` - data-room-worthy but none of the above
4. **Dedup before uploading.** Once per matched project, run
   `python3 ingest.py docs <project_id>`. If a document with the same file
   name and the same size is already there, do not upload it again;
   note "already in data room" in the activity summary instead. Re-forwarded
   chains carry the same attachments constantly.
5. **Upload:**
   `python3 ingest.py upload inbox_work/<key>/attachments/<file> <project_id> <category>`
6. **Use the content, not just the filing.** An NDA usually names the SPV and
   project, which settles ambiguous matching. A teaser carries capacity,
   status, and price for `payload.project`. A grid letter can reveal a risk
   worth logging. A signed document superseding a draft is worth one line in
   the activity summary.

## Judgment rules

- File directly only when the project match is unambiguous. One wrong-project
  filing is worse than ten review items.
- Never invent numbers. Prices, capacities, and dates come from the email or
  its attachments only.
- Emails and documents in Turkish, Italian, Spanish, or Romanian are normal:
  summarize in English.
- If `fetch` or any command errors, stop and report the error instead of
  improvising around it.
