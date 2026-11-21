# Turn on "your design is ready" emails (Resend)

Run these in **your own terminal** (not in a chat), from any folder.
The key never needs to appear on screen or in a file.

## 1. Store the Resend API key in Secret Manager

Paste the key when the cursor waits (nothing is shown as you paste), then press Enter:

```bash
read -s RESEND_KEY && printf '%s' "$RESEND_KEY" | gcloud secrets versions add studio-resend-api-key --project YOUR_PROJECT_ID --data-file=- && unset RESEND_KEY
```

## 2. Check it was stored (prints a version number, not the key)

```bash
gcloud secrets versions list studio-resend-api-key --project YOUR_PROJECT_ID
```

You should see one row with `STATE: enabled`. No redeploy is needed: the worker reads the key at
the start of every job.

## 3. Send yourself a test: one real background job that emails you when done

Replace `YOUR_EMAIL` with the address you signed up to Resend with (see the note below).
Keep the brief free of commas; they separate the arguments.

```bash
gcloud run jobs execute studio-worker --project YOUR_PROJECT_ID --region asia-south1 \
  --args='--smoke,A square 1080x1080 post saying "Email test",YOUR_EMAIL' --wait
```

It takes about 5 minutes. When it finishes you should get an email titled
**"Ready: A square 1080x1080 post saying "Email test""** (check spam the first time).

If no email arrives, show the worker's last lines:

```bash
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="studio-worker"' \
  --project YOUR_PROJECT_ID --freshness 15m --limit 50 --format='value(textPayload)' | grep -iE "email|resend|succeeded|failed"
```

- `email sent`: Resend accepted it; check spam and the Resend dashboard (Emails).
- `email skipped` with `no Resend key`: step 1 didn't store the key; run it again.
- `Resend rejected the email: 403`: the address isn't your Resend account's own address (see below).

## Note: who can receive emails right now

Until you verify a domain, Resend's test sender (`onboarding@resend.dev`) can only deliver to
**the email address of your Resend account**. To email your customers:

1. In Resend: Domains, then Add domain, and add the DNS records it shows at your domain registrar.
2. Once it shows Verified, tell Claude the domain (for example `studio.example.com`). Claude will
   switch the sender to something like `Studio <hello@studio.example.com>` in Terraform and redeploy.

## Rotating the key later

Run step 1 again with the new key (it adds a new version; the worker always uses the latest), then
disable the old one in Resend.
