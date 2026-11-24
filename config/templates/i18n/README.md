# Translated templates

One folder per language: `fr, de, es, it, pt, nl, pl, ar, ru, tr`. Each folder can hold
any of `email_first.txt`, `email_followup.txt` and `whatsapp_after_reply.txt` with the same
`{placeholders}` as the English templates in `config/templates/`.

If a translated file is missing, `leads.outreach.templates` falls back to English, so a
language can go live one template at a time.

TODO (build step 7): add translations, each checked by a native speaker before use.
Also add the translated `website_line` phrases in `src/leads/outreach/templates.py`,
and the translated opt-out sentence ("reply 'no thanks'…") must match a phrase in
`leads.outreach.gmail_sync.OPT_OUT_PHRASES`.
