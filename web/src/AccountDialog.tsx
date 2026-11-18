import { useEffect, useRef, useState } from 'react';
import { X } from '@phosphor-icons/react';
import { api, setToken } from './api';
import type { Config } from './types';

export function AccountDialog({ config, close, changed }: { config: Config; close: () => void; changed: () => Promise<void> }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} className="intake-dialog account-dialog" onCancel={close} onClick={e => { if (e.target === e.currentTarget) close(); }}>
    <div className="modal-heading"><div><p className="eyebrow">DEMO ACCOUNTS</p><h2>Sign in / Switch account</h2><p>Current identity: {config.actor}</p></div><button className="icon-button" aria-label="Close account switcher" onClick={close}><X size={20} /></button></div>
    <form onSubmit={async e => {
      e.preventDefault(); setBusy(true); setError('');
      const form = e.currentTarget;
      const values = new FormData(form);
      try { await api('/api/session/login', {method:'POST', body:JSON.stringify({username:values.get('username'), password:values.get('password')})}); setToken(''); form.reset(); await changed(); close(); }
      catch (err) { setError((err as Error).message); } finally { setBusy(false); }
    }}>
      {error && <p role="alert" className="error-box">{error}</p>}
      <label>User ID<select name="username" autoFocus defaultValue="creator">{config.demo_accounts?.map(account => <option key={account.username} value={account.username}>{account.username} — {account.label}</option>)}</select></label>
      <label>Shared demo password<input type="password" name="password" autoComplete="current-password" required maxLength={200} /></label>
      <p className="helper">Each account has different permissions. Sessions last eight hours. The shared password is for this demonstration only.</p>
      <div className="account-actions"><button className="primary" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button><button type="button" className="secondary" disabled={busy} onClick={async () => { setBusy(true); try { await api('/api/session/logout', {method:'POST'}); setToken(''); await changed(); close(); } catch(err) {setError((err as Error).message);} finally {setBusy(false);} }}>Sign out</button></div>
    </form>
  </dialog>;
}
