/** The web intake form. It posts to /requests and shows the tracking ID. */
export const intakeFormHtml = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Software Factory: new request</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.5 system-ui, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  p.lead { margin-top: 0; opacity: 0.75; }
  label { display: block; font-weight: 600; margin-top: 16px; }
  small { font-weight: 400; opacity: 0.75; }
  input, textarea, select { width: 100%; box-sizing: border-box; font: inherit; padding: 8px; margin-top: 4px; }
  textarea { min-height: 120px; }
  button { margin-top: 20px; font: inherit; font-weight: 600; padding: 10px 18px; cursor: pointer; }
  #result { margin-top: 20px; padding: 12px; border: 1px solid; border-radius: 6px; display: none; }
</style>
</head>
<body>
<h1>New software request</h1>
<p class="lead">You get a tracking ID as soon as the request is received.</p>
<form id="intake">
  <label>Your name or email <input name="requestor" required maxlength="200"></label>
  <label>Title <input name="title" required maxlength="200"></label>
  <label>Description <textarea name="description" required></textarea></label>
  <label>Acceptance criteria <small>(one per line)</small>
    <textarea name="acceptanceCriteria" style="min-height: 80px"></textarea></label>
  <label>Priority
    <select name="priority">
      <option value="low">Low</option>
      <option value="normal" selected>Normal</option>
      <option value="high">High</option>
      <option value="urgent">Urgent</option>
    </select></label>
  <button type="submit">Submit request</button>
</form>
<div id="result" role="status"></div>
<script>
  const form = document.getElementById('intake');
  const result = document.getElementById('result');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const body = {
      source: 'web_form',
      requestor: data.get('requestor'),
      title: data.get('title'),
      description: data.get('description'),
      priority: data.get('priority'),
      acceptanceCriteria: String(data.get('acceptanceCriteria') || '')
        .split('\\n').map((line) => line.trim()).filter(Boolean),
    };
    result.style.display = 'block';
    result.textContent = 'Submitting...';
    try {
      const response = await fetch('/requests', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });
      const json = await response.json();
      if (!response.ok) {
        result.textContent = 'Could not submit: ' + (json.error || response.status);
        return;
      }
      result.textContent = (json.duplicate ? 'Already received as ' : 'Received. Tracking ID: ')
        + json.trackingId;
      if (!json.duplicate) form.reset();
    } catch (err) {
      result.textContent = 'Could not reach the server.';
    }
  });
</script>
</body>
</html>
`;
