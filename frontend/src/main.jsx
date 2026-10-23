import React, {useCallback, useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import CodeMirror from '@uiw/react-codemirror';
import {python} from '@codemirror/lang-python';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import DOMPurify from 'dompurify';
import { BookOpen, Plus, Play, Square, RefreshCcw, Save, Download, Upload, Trash2, ChevronUp, ChevronDown, Code2, FileText, Cpu, LogOut, Users, Shield, X, Settings2, RotateCcw, CircleHelp, Activity, Check, AlertCircle, NotebookPen, Eye, Pencil, CircleStop, Zap, Search, Copy } from 'lucide-react';
import './style.css';

const pyExtensions = [python()];
const randomId = () => { if (globalThis.crypto?.randomUUID) return crypto.randomUUID().replaceAll('-', '').slice(0, 20); const bytes = new Uint8Array(10); crypto.getRandomValues(bytes); return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join(''); };
let csrfToken = '';
async function api(path, options={}) {
  const headers = {...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (options.method && !['GET','HEAD'].includes(options.method)) headers['X-CSRF-Token'] = csrfToken;
  const res = await fetch('/api'+path, {...options, headers, credentials:'include'});
  if (!res.ok) {
    let message;
    try { message = (await res.json()).detail; } catch { message = res.statusText; }
    throw new Error(typeof message === 'string' ? message : JSON.stringify(message));
  }
  if ((res.headers.get('content-type') || '').includes('application/json')) return res.json();
  return res;
}
function sourceText(src) { return Array.isArray(src) ? src.join('') : (src || ''); }
function normalize(doc) { return {...doc, cells: (doc.cells || []).map(c=>{const {outputs, execution_count, ...rest}=c; return c.cell_type==='code' ? {...rest, source: sourceText(c.source), outputs: outputs || [], execution_count: execution_count ?? null} : {...rest, source: sourceText(c.source)};})}; }
function HeaderButton({children, onClick, className='', disabled=false, title='', ...props}) {return <button {...props} title={title} onClick={onClick} disabled={disabled} className={'btn '+className}>{children}</button>}
function newCell(type='code') { return {cell_type:type, id:randomId(), metadata:{}, source:'', ...(type==='code'?{outputs:[],execution_count:null}:{})}; }

function Login({onLogin}) {
  const [email, setEmail] = useState(''); const [password, setPassword] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  async function login(e) { e.preventDefault(); setBusy(true);setError('');try { const a=await api('/auth/login',{method:'POST',body:JSON.stringify({email,password})});csrfToken=a.csrf; onLogin(a.user); }catch(e){setError(e.message)}finally{setBusy(false)} }
  return <div className="auth-layout"><div className="glow glow-1"/><div className="glow glow-2"/><form className="login-card" onSubmit={login}>
    <div className="brand-big"><span className="logo"><NotebookPen size={26}/></span><span>Nebula<span className="brand-thin">Notebook</span></span></div>
    <p className="subtitle">A secure interactive workspace for Python experiments.</p>
    <label>Email<input type="email" value={email} autoComplete="username" onChange={e=>setEmail(e.target.value)} required placeholder="you@example.com" /></label>
    <label>Password<input type="password" autoComplete="current-password" value={password} onChange={e=>setPassword(e.target.value)} required placeholder="Your password"/></label>
    {error && <div className="alert"><AlertCircle size={16}/>{error}</div>}
    <button className="btn primary large" disabled={busy} type="submit">{busy?'Signing in...':'Sign in'} <span>→</span></button>
    <div className="login-note"><Shield size={14}/> Accounts are managed by an administrator.</div>
  </form></div>;
}
function Modal({title, onClose, children}) { return <div className="overlay" onMouseDown={onClose}><section className="modal" onMouseDown={e=>e.stopPropagation()}><header className="modal-head"><strong>{title}</strong><button className="icon-btn" onClick={onClose}><X size={18}/></button></header>{children}</section></div>}

function Dashboard({user, open, logout}) {
 const [items,setItems] = useState([]); const [error,setError]=useState(''); const [search,setSearch]=useState('');const [newTitle,setNewTitle]=useState('Untitled notebook'); const [createOpen,setCreateOpen]=useState(false); const [adminOpen,setAdminOpen]=useState(false);
 const [users,setUsers]=useState([]);const [newUser,setNewUser]=useState({email:'',password:'',is_admin:false});const [audit,setAudit]=useState([]);
 const fileRef=useRef();
 const refresh=useCallback(async()=>{try{setItems(await api('/notebooks'))}catch(e){setError(e.message)}},[]);
 useEffect(()=>{refresh()},[refresh]);
 async function create(){try{const n=await api('/notebooks',{method:'POST',body:JSON.stringify({title:newTitle})}); setCreateOpen(false);open(n.id)}catch(e){setError(e.message)}}
 async function importFile(file){if(!file)return;const data=new FormData();data.append('file',file);try{const n=await api('/notebooks/import',{method:'POST',body:data});open(n.id)}catch(e){setError(e.message)}finally{if(fileRef.current)fileRef.current.value=''}}
 async function loadAdmin(){try{setUsers(await api('/admin/users'));setAudit(await api('/admin/audit'));setAdminOpen(true)}catch(e){setError(e.message)}}
 async function createUser(e){e.preventDefault();try{await api('/admin/users',{method:'POST',body:JSON.stringify(newUser)});setNewUser({email:'',password:'',is_admin:false});setUsers(await api('/admin/users'))}catch(e){setError(e.message)}}
 async function toggleUser(u){try{await api('/admin/users/'+u.id,{method:'PATCH',body:JSON.stringify({is_active:!u.is_active})});setUsers(await api('/admin/users'))}catch(e){setError(e.message)}}
 return <div className="app-shell"><aside className="sidebar"><div className="brand"><span className="logo"><NotebookPen size={21}/></span><span>Nebula <strong>Notebook</strong></span></div><div className="nav-item active"><BookOpen size={18}/> My notebooks</div><div className="nav-bottom"><div className="user-small"><div className="avatar">{user.email[0].toUpperCase()}</div><div><strong>{user.email.split('@')[0]}</strong><small>{user.is_admin?'Administrator':'Member'}</small></div></div>{user.is_admin&&<button className="nav-item" onClick={loadAdmin}><Shield size={18}/> Administration</button>}<button className="nav-item" onClick={logout}><LogOut size={18}/> Sign out</button></div></aside>
 <main className="dashboard"><div className="dash-top"><div><small className="eyebrow">WORKSPACE</small><h1>Your notebooks<span className="dot">.</span></h1><p className="muted">Create, experiment, and share interactive Python notebooks.</p></div><div className="top-actions"><HeaderButton onClick={()=>fileRef.current?.click()}><Upload size={16}/> Import .ipynb</HeaderButton><HeaderButton className="primary" onClick={()=>setCreateOpen(true)}><Plus size={17}/> New notebook</HeaderButton></div></div>
 {error&&<div className="alert"><AlertCircle size={16}/>{error}<button onClick={()=>setError('')}>×</button></div>}
 <input type="file" accept=".ipynb,application/json" style={{display:'none'}} ref={fileRef} onChange={e=>importFile(e.target.files?.[0])}/>
 <div className="section-heading"><h2>Recent notebooks <span>{items.length}</span></h2><div className="search"><Search size={15}/><input placeholder="Search notebooks" value={search} onChange={e=>setSearch(e.target.value)}/></div></div>
 <div className="notebook-grid">{items.filter(n=>n.title.toLowerCase().includes(search.toLowerCase())).map(n=><button key={n.id} className="nb-card" onClick={()=>open(n.id)}><div className="nb-card-icon"><Code2 size={23}/></div><h3>{n.title}</h3><p>Python 3 · Notebook</p><div className="card-foot"><span className={'role '+n.role}>{n.role}</span><small>{n.updated_at?new Date(n.updated_at).toLocaleDateString():''}</small></div></button>)}<button className="new-card" onClick={()=>setCreateOpen(true)}><Plus size={26}/><strong>New notebook</strong><span>Start from scratch</span></button></div>
 </main>{createOpen&&<Modal title="Create notebook" onClose={()=>setCreateOpen(false)}><div className="modal-body"><label>Notebook name<input value={newTitle} onChange={e=>setNewTitle(e.target.value)} onKeyDown={e=>{if(e.key==='Enter')create()}} autoFocus/></label><div className="modal-actions"><HeaderButton onClick={()=>setCreateOpen(false)}>Cancel</HeaderButton><HeaderButton className="primary" onClick={create}>Create notebook</HeaderButton></div></div></Modal>}
 {adminOpen&&<Modal title="User administration" onClose={()=>setAdminOpen(false)}><div className="modal-body"><h4>Create user</h4><form className="admin-form" onSubmit={createUser}><input type="email" placeholder="Email" value={newUser.email} onChange={e=>setNewUser({...newUser,email:e.target.value})} required/><input type="password" minLength="12" placeholder="Password (12+ chars)" value={newUser.password} onChange={e=>setNewUser({...newUser,password:e.target.value})} required/><label className="inline-check"><input type="checkbox" checked={newUser.is_admin} onChange={e=>setNewUser({...newUser,is_admin:e.target.checked})}/> Admin</label><HeaderButton className="primary" type="submit">Create user</HeaderButton></form><h4>Users</h4><div className="scroll-list">{users.map(u=><div className="list-line" key={u.id}><span>{u.email}<small>{u.is_admin?' · admin':''}</small></span><button className="small-link" disabled={u.id===user.id} onClick={()=>toggleUser(u)}>{u.is_active?'Deactivate':'Activate'}</button></div>)}</div><h4>Latest audit events</h4><div className="scroll-list">{audit.slice(0,15).map((a,i)=><div className="list-line" key={i}><span>{a.event}<small> · {a.target_id.slice(0,8)}</small></span><small>{new Date(a.created_at).toLocaleString()}</small></div>)}</div></div></Modal>}
 </div>
}

function RichOutput({output}) {
 if(output.output_type==='stream') return <pre className="output-text">{sourceText(output.text)}</pre>;
 if(output.output_type==='error') return <pre className="output-error">{(output.traceback||[]).join('\n').replace(/\x1b\[[0-9;]*m/g,'')}</pre>;
 const d=output.data||{};
 if(d['image/png']) return <div className="output-image"><img alt="Cell output" src={'data:image/png;base64,'+sourceText(d['image/png'])}/></div>;
 if(d['image/jpeg']) return <div className="output-image"><img alt="Cell output" src={'data:image/jpeg;base64,'+sourceText(d['image/jpeg'])}/></div>;
 if(d['text/html']) return <div className="output-html" dangerouslySetInnerHTML={{__html:DOMPurify.sanitize(sourceText(d['text/html']),{FORBID_TAGS:['style','script','iframe','object','embed','form']})}}/>;
 if(d['text/plain']) return <pre className="output-text">{sourceText(d['text/plain'])}</pre>;
 return <div className="muted">Unsupported output format: {Object.keys(d).join(', ')}</div>;
}

function EditorCell({cell, index, canEdit, update, run, remove, move, add, running}) {
 const [markdownEdit,setMarkdownEdit]=useState(!cell.source);
 return <div className={'cell '+(running?'cell-running':'')} id={'cell-'+cell.id}><div className="cell-toolbar"><div className="cell-type"><span className={'cell-dot '+cell.cell_type}/>{cell.cell_type==='code'?'Python':'Markdown'} <span className="execution-count">{cell.execution_count==null?'':`[${cell.execution_count}]`}</span></div><div className="cell-controls">{canEdit&&<><button title="Move up" onClick={()=>move(index,-1)}><ChevronUp size={15}/></button><button title="Move down" onClick={()=>move(index,1)}><ChevronDown size={15}/></button><button title="Insert below" onClick={()=>add(index+1,cell.cell_type)}><Plus size={15}/></button><button title="Delete cell" onClick={()=>remove(index)}><Trash2 size={15}/></button></>}</div></div>
 <div className="cell-content">{cell.cell_type==='code'?<div className="code-area"><div className="run-gutter"><button aria-label="Run cell" title="Run cell" className="run-cell" disabled={!canEdit||running} onClick={()=>run(cell)}><Play size={17} fill="currentColor"/></button></div><CodeMirror value={sourceText(cell.source)} readOnly={!canEdit} editable={canEdit} onChange={v=>update(index,{source:v})} extensions={pyExtensions} theme="dark" basicSetup={{foldGutter:false,highlightActiveLine:true,highlightActiveLineGutter:false}}/></div>:
 <div className="markdown-area">{canEdit&&markdownEdit?<textarea rows={Math.max(3,sourceText(cell.source).split('\n').length+1)} value={sourceText(cell.source)} onChange={e=>update(index,{source:e.target.value})} onBlur={()=>setMarkdownEdit(false)} placeholder="Write Markdown here..."/>:<div className="markdown-preview" onDoubleClick={()=>canEdit&&setMarkdownEdit(true)}><ReactMarkdown remarkPlugins={[remarkGfm]}>{sourceText(cell.source)||'*Double-click to edit Markdown*'}</ReactMarkdown>{canEdit&&<button className="edit-markdown" onClick={()=>setMarkdownEdit(true)}><Pencil size={14}/> Edit</button>}</div>}</div>}
 {cell.cell_type==='code'&&(cell.outputs||[]).length>0&&<div className="output-area">{cell.outputs.map((o,i)=><RichOutput output={o} key={i}/>)}</div>}
 </div></div>;
}

function ShareDialog({id,onClose}) {
 const [shares,setShares]=useState([]),[email,setEmail]=useState(''),[role,setRole]=useState('viewer'),[error,setError]=useState('');
 const load=useCallback(async()=>{try{setShares(await api(`/notebooks/${id}/shares`))}catch(e){setError(e.message)}},[id]);
 useEffect(()=>{load()},[load]);
 async function share(){try{await api(`/notebooks/${id}/shares`,{method:'PUT',body:JSON.stringify({email,role})});setEmail('');await load()}catch(e){setError(e.message)}}
 async function revoke(uid){try{await api(`/notebooks/${id}/shares/${uid}`,{method:'DELETE'});await load()}catch(e){setError(e.message)}}
 return <Modal title="Share notebook" onClose={onClose}><div className="modal-body"><p className="muted">Only invited users can access this notebook. Editors may execute Python code in its runtime; viewers cannot.</p><div className="share-form"><input type="email" placeholder="Person's email" value={email} onChange={e=>setEmail(e.target.value)}/><select value={role} onChange={e=>setRole(e.target.value)}><option value="viewer">Viewer</option><option value="editor">Editor</option></select><HeaderButton className="primary" onClick={share}>Share</HeaderButton></div>{error&&<div className="alert">{error}</div>}<h4>People with access</h4>{shares.length===0&&<p className="muted">Not shared with anyone.</p>}{shares.map(s=><div className="list-line" key={s.user_id}><span>{s.email} <small>· {s.role}</small></span><button className="small-link" onClick={()=>revoke(s.user_id)}>Revoke</button></div>)}</div></Modal>;
}


function FilesDialog({id,onClose}) {
 const [files,setFiles]=useState([]),[error,setError]=useState(''),[busy,setBusy]=useState(false); const fileRef=useRef();
 const load=useCallback(async()=>{try{setFiles(await api(`/notebooks/${id}/files`))}catch(e){setError(e.message)}},[id]);
 useEffect(()=>{load()},[load]);
 async function upload(file){if(!file)return;setBusy(true);setError('');try{const form=new FormData();form.append('file',file);await api(`/notebooks/${id}/files`,{method:'POST',body:form});await load()}catch(e){setError(e.message)}finally{setBusy(false);if(fileRef.current)fileRef.current.value=''}}
 async function download(name){try{const res=await api(`/notebooks/${id}/files/${encodeURIComponent(name)}`);const blob=await res.blob();const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(link.href),3000)}catch(e){setError(e.message)}}
 async function remove(name){if(!confirm(`Delete ${name} from this notebook workspace?`))return;try{await api(`/notebooks/${id}/files/${encodeURIComponent(name)}`,{method:'DELETE'});await load()}catch(e){setError(e.message)}}
 return <Modal title="Notebook files" onClose={onClose}><div className="modal-body"><p className="muted">Upload datasets into the persistent <code>/workspace</code> folder. Python can read them by filename. Files remain after the runtime disconnects.</p><input ref={fileRef} type="file" style={{display:'none'}} onChange={e=>upload(e.target.files?.[0])}/><HeaderButton className="primary" disabled={busy} onClick={()=>fileRef.current?.click()}><Upload size={15}/>{busy?'Uploading...':'Upload file (max 25 MB)'}</HeaderButton>{error&&<div className="alert">{error}</div>}<h4>Workspace files</h4><div className="scroll-list">{files.length===0&&<p className="muted">No files yet.</p>}{files.map(file=><div className="list-line" key={file.name}><span><FileText size={14} style={{verticalAlign:'middle',marginRight:7}}/>{file.name}<small> · {(file.size/1024).toFixed(1)} KB</small></span><span className="file-actions"><button className="small-link" onClick={()=>download(file.name)}>Download</button><button className="small-link" onClick={()=>remove(file.name)}>Delete</button></span></div>)}</div><p className="muted">Only simple filenames are accepted. Existing files cannot be overwritten via upload.</p></div></Modal>
}

function NotebookEditor({id,user,back,logout}) {
 const [entry,setEntry]=useState(null),[doc,setDoc]=useState(null),[title,setTitle]=useState(''),[runtime,setRuntime]=useState({mode:null,status:'stopped'}),[caps,setCaps]=useState(null),[choice,setChoice]=useState('cpu');
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[saving,setSaving]=useState('saved'),[sharing,setSharing]=useState(false),[filesOpen,setFilesOpen]=useState(false),[busy,setBusy]=useState(false),[channelReady,setChannelReady]=useState(false);
 const [dirty,setDirty]=useState(false),[revision,setRevision]=useState(0),[saveTick,setSaveTick]=useState(0),[runningIds,setRunningIds]=useState({});
 const socket=useRef(null),versions=useRef(1),revisions=useRef(0),savingRef=useRef(false),pending=useRef(new Map()),latestDoc=useRef(null),latestTitle=useRef('');
 const canEdit = !!entry && entry.role!=='viewer';
 function editDoc(fn){setDoc(prev=>{const next=typeof fn==='function'?fn(prev):fn;latestDoc.current=next;return next});revisions.current++;setRevision(revisions.current);setDirty(true)}
 function updateCell(index,patch){editDoc(d=>({...d,cells:d.cells.map((c,i)=>i===index?{...c,...patch}:c)}))}
 const fetchRuntime=useCallback(async()=>{try{setRuntime(await api(`/notebooks/${id}/runtime`))}catch{}},[id]);
 useEffect(()=>{let active=true; (async()=>{try{const n=await api(`/notebooks/${id}`);if(!active)return;setEntry(n);setDoc(normalize(n.document));latestDoc.current=normalize(n.document);setTitle(n.title);latestTitle.current=n.title;versions.current=n.version;setRuntime(n.runtime||{mode:null,status:'stopped'});if(n.runtime?.status==='ready')connectChannels();setCaps(await api('/runtime/capabilities'))}catch(e){if(active)setError(e.message)}})();return()=>{active=false;socket.current?.close()}},[id]);
 const save=useCallback(async()=>{if(!canEdit||savingRef.current||!dirty||!latestDoc.current)return;const at=revisions.current;const snapshot=latestDoc.current;const name=latestTitle.current; savingRef.current=true;setSaving('saving');try{const result=await api(`/notebooks/${id}`,{method:'PUT',body:JSON.stringify({document:snapshot,title:name,version:versions.current})});versions.current=result.version;if(revisions.current===at){setDirty(false);setSaving('saved')}else{setSaving('unsaved')}}catch(e){setError(e.message);setSaving('error')}finally{savingRef.current=false;if(revisions.current!==at)setSaveTick(x=>x+1)}},[canEdit,dirty,id]);
 useEffect(()=>{if(!dirty||!canEdit)return;const t=setTimeout(save,1100);return()=>clearTimeout(t)},[revision,saveTick,dirty,save,canEdit]);
 useEffect(()=>{if(!dirty||!canEdit)return;const handler=e=>{e.preventDefault();e.returnValue='';};window.addEventListener('beforeunload',handler);return()=>window.removeEventListener('beforeunload',handler)},[dirty,canEdit]);
 function changeTitle(t){setTitle(t);latestTitle.current=t;revisions.current++;setRevision(revisions.current);setDirty(true)}
 function move(i,delta){editDoc(d=>{const arr=[...d.cells];const j=i+delta;if(j<0||j>=arr.length)return d;[arr[i],arr[j]]=[arr[j],arr[i]];return {...d,cells:arr}})}
 function add(index,type){editDoc(d=>({...d,cells:[...d.cells.slice(0,index),newCell(type),...d.cells.slice(index)]}))}
 function remove(index){editDoc(d=>({...d,cells:d.cells.filter((_,i)=>i!==index)}))}
 function receiveMessage(raw){let m;try{m=JSON.parse(raw)}catch{return}const kind=m.header?.msg_type, req=m.parent_header?.msg_id;const cellId=pending.current.get(req);if(!cellId)return;
   if(kind==='status'&&m.content?.execution_state==='idle'){pending.current.delete(req);setRunningIds(s=>{const x={...s};delete x[cellId];return x});return}
   if(kind==='status'||kind==='execute_reply')return;
   editDoc(d=>({...d,cells:d.cells.map(c=>{if(c.id!==cellId)return c;let changes={};if(kind==='execute_input')changes.execution_count=m.content?.execution_count;
      if(['stream','execute_result','display_data','error'].includes(kind)){const out={...m.content};out.output_type=kind==='execute_result'?'execute_result':kind==='display_data'?'display_data':kind==='error'?'error':'stream';changes.outputs=[...(c.outputs||[]),out];}return {...c,...changes}})}));
 }
 function connectChannels(){setChannelReady(false);const proto=location.protocol==='https:'?'wss:':'ws:';const w=new WebSocket(`${proto}//${location.host}/api/notebooks/${id}/channels`);w.onopen=()=>setChannelReady(true);w.onmessage=e=>receiveMessage(e.data);w.onerror=()=>setNotice('Kernel WebSocket error — check connection.');w.onclose=()=>{if(socket.current===w){socket.current=null;setChannelReady(false);setRunningIds({});}};socket.current=w;return w;}
 async function start(){setBusy(true);setError('');try{if(choice==='gpu'&&!caps?.gpu_selectable)throw new Error('GPU unavailable on this server');const r=await api(`/notebooks/${id}/runtime`,{method:'POST',body:JSON.stringify({mode:choice})});setRuntime(r);socket.current?.close();connectChannels();setNotice(`${choice.toUpperCase()} runtime connected`)}catch(e){setError(e.message)}finally{setBusy(false)}}
 async function stop(){setBusy(true);try{socket.current?.close();await api(`/notebooks/${id}/runtime`,{method:'DELETE'});setRuntime({mode:null,status:'stopped'});pending.current.clear();setRunningIds({})}catch(e){setError(e.message)}finally{setBusy(false)}}
 async function interrupt(){try{await api(`/notebooks/${id}/runtime/interrupt`,{method:'POST'})}catch(e){setError(e.message)}}
 function run(cell){const ws=socket.current;if(!canEdit||runtime.status!=='ready'||!channelReady||!ws||ws.readyState!==WebSocket.OPEN){setError('Connect the runtime and wait until the Kernel channel is open');return}const msgId=randomId();pending.current.set(msgId,cell.id);setRunningIds(s=>({...s,[cell.id]:true}));editDoc(d=>({...d,cells:d.cells.map(c=>c.id===cell.id?{...c,outputs:[],execution_count:null}:c)}));
   ws.send(JSON.stringify({channel:'shell',header:{msg_id:msgId,username:'nebula',session:randomId(),date:new Date().toISOString(),msg_type:'execute_request',version:'5.3'},parent_header:{},metadata:{},content:{code:sourceText(cell.source),silent:false,store_history:true,user_expressions:{},allow_stdin:false,stop_on_error:true},buffers:[]}));
 }
 async function runAll(){if(!doc)return;for(const c of doc.cells){if(c.cell_type==='code')run(c)} }
 async function exportNb(){try{if(dirty)await save();const res=await api(`/notebooks/${id}/export`);const blob=await res.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=(title||'notebook').replace(/[^a-z\d_-]/gi,'_')+'.ipynb';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),5000)}catch(e){setError(e.message)}}
 async function deleteNb(){if(!confirm('Delete this notebook? Its persistent workspace files will be retained.'))return;try{await api(`/notebooks/${id}`,{method:'DELETE'});back()}catch(e){setError(e.message)}}
 if(!entry||!doc)return <div className="editor-loading"><NotebookPen size={28}/>{error||'Loading notebook...' }<button className="btn" onClick={back}>Back</button></div>;
 return <div className="editor-shell"><header className="editor-header"><div className="header-brand"><button className="logo compact" onClick={back} title="Back to notebooks"><NotebookPen size={19}/></button><div><input className="title-input" value={title} disabled={!canEdit} onChange={e=>changeTitle(e.target.value)} aria-label="Notebook title"/><div className="subtitle-small"><span className="bread" onClick={back}>My notebooks</span> / Python 3 <span className="bullet">·</span> <span className="save-state">{saving==='saving'?'Saving...':dirty?'Unsaved changes':'All changes saved'}</span></div></div></div><div className="head-buttons"><HeaderButton disabled={!canEdit} onClick={save} title="Save"><Save size={16}/> Save</HeaderButton><HeaderButton onClick={exportNb}><Download size={16}/> Export</HeaderButton>{canEdit&&<HeaderButton onClick={()=>setFilesOpen(true)}><FileText size={16}/> Files</HeaderButton>}{entry.role==='owner'&&<HeaderButton onClick={()=>setSharing(true)}><Users size={16}/> Share</HeaderButton>}<button className="avatar top-avatar" title={user.email} onClick={logout}>{user.email[0].toUpperCase()}</button></div></header>
 <div className="editor-toolbar"><div className="toolbar-main"><HeaderButton className="toolbar-primary" disabled={!canEdit} onClick={()=>add(doc.cells.length,'code')}><Plus size={16}/> Code</HeaderButton><HeaderButton disabled={!canEdit} onClick={()=>add(doc.cells.length,'markdown')}><FileText size={16}/> Text</HeaderButton><div className="sep"/><HeaderButton disabled={!canEdit||runtime.status!=='ready'||!channelReady} onClick={runAll}><Play size={16}/> Run all</HeaderButton><HeaderButton disabled={!canEdit||runtime.status!=='ready'} onClick={interrupt}><Square size={15}/> Interrupt</HeaderButton><span className="role-pill"><Shield size={14}/>{entry.role}</span></div><div className="runtime-controls"><span className={'runtime-indicator '+runtime.status}><span/>{runtime.status==='ready'?'Connected':runtime.status==='stopped'?'Disconnected':runtime.status}</span>{runtime.status==='ready'?<><span className="runtime-mode"><Cpu size={15}/> {runtime.mode?.toUpperCase()} {channelReady?"· Kernel ready":"· Kernel disconnected"}</span>{!channelReady&&<HeaderButton onClick={connectChannels}>Reconnect</HeaderButton>}<HeaderButton onClick={stop} disabled={!canEdit||busy}><CircleStop size={16}/> Disconnect</HeaderButton></>:<><select value={choice} onChange={e=>setChoice(e.target.value)} disabled={!canEdit||busy}><option value="cpu">CPU</option>{caps?.gpu_selectable&&<option value="gpu">GPU · {caps.gpu.name?.slice(0,23)}</option>}</select><HeaderButton className="primary" onClick={start} disabled={!canEdit||busy||!caps?.cpu}>{busy?'Connecting...':'Connect'} <Zap size={16}/></HeaderButton></>}</div></div>
 <div className="editor-layout"><main className="cells-panel"><div className="notebook-intro"><small className="eyebrow">INTERACTIVE NOTEBOOK</small><h1>{title}</h1><p>{canEdit?'Write code, explore results, and keep your research in one place.':'Read-only notebook. Contact the owner for editor permission.'}</p></div>{error&&<div className="alert"><AlertCircle size={16}/>{error}<button onClick={()=>setError('')}>×</button></div>}{notice&&<div className="notice"><Check size={15}/>{notice}<button onClick={()=>setNotice('')}>×</button></div>}
 {doc.cells.map((cell,i)=><EditorCell key={cell.id} cell={cell} index={i} canEdit={canEdit} update={updateCell} run={run} remove={remove} move={move} add={add} running={!!runningIds[cell.id]}/>)}
 {canEdit&&<div className="end-cell-actions"><HeaderButton onClick={()=>add(doc.cells.length,'code')}><Plus size={16}/> Code cell</HeaderButton><HeaderButton onClick={()=>add(doc.cells.length,'markdown')}><Plus size={16}/> Text cell</HeaderButton></div>}
 <div className="notebook-end">End of notebook <span>·</span> {doc.cells.length} cells</div></main>
 <aside className="details-panel"><div className="details-header"><Activity size={17}/> Environment</div><div className="detail-row"><span>Language</span><strong>Python 3</strong></div><div className="detail-row"><span>Status</span><strong>{runtime.status}</strong></div><div className="detail-row"><span>Compute</span><strong>{runtime.mode?.toUpperCase()||'None'}</strong></div><div className="detail-row"><span>Autosave</span><strong>Enabled</strong></div><div className="detail-row"><span>Cells</span><strong>{doc.cells.length}</strong></div><div className="detail-divider"/><div className="details-header"><Cpu size={17}/> GPU availability</div>{caps?.gpu_selectable?<div className="gpu-good"><Check size={15}/> {caps.gpu.name}</div>:<div className="gpu-off"><AlertCircle size={15}/> GPU not available<div className="gpu-hint">{caps?.gpu?.reason || 'Probing local runtime...'}</div></div>}{!caps?.cpu&&<div className="gpu-off">CPU image missing — build the runtime image first.</div>}<button className="small-link refresh" onClick={async()=>{try{const c=await api('/runtime/capabilities?refresh=true');setCaps(c)}catch(e){setError(e.message)}}}><RefreshCcw size={14}/> Refresh status</button><div className="detail-divider"/><div className="details-header"><Shield size={17}/> Permissions</div><p className="detail-help">{entry.role==='owner'?'You own this notebook and can manage sharing.':entry.role==='editor'?'You can edit and execute cells.':'You can view saved content only.'}</p>{entry.role==='owner'&&<HeaderButton className="danger small" onClick={deleteNb}><Trash2 size={15}/> Delete notebook</HeaderButton>}</aside></div>
 {sharing&&<ShareDialog id={id} onClose={()=>setSharing(false)}/>}
 {filesOpen&&<FilesDialog id={id} onClose={()=>setFilesOpen(false)}/>}</div>
}

function App(){const [user,setUser]=useState(null),[loading,setLoading]=useState(true),[opened,setOpened]=useState(null);useEffect(()=>{api('/auth/me').then(r=>{csrfToken=r.csrf;setUser(r.user)}).catch(()=>{}).finally(()=>setLoading(false))},[]);async function logout(){try{await api('/auth/logout',{method:'POST'})}catch{}csrfToken='';setUser(null);setOpened(null)}if(loading)return <div className="editor-loading"><NotebookPen size={30}/> Loading Nebula...</div>;return user?(opened?<NotebookEditor key={opened} id={opened} user={user} back={()=>setOpened(null)} logout={logout}/>:<Dashboard user={user} open={setOpened} logout={logout}/>):<Login onLogin={setUser}/>}
createRoot(document.getElementById('root')).render(<App/>);
