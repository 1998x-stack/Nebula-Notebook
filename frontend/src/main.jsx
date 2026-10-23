import React, {useCallback, useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import CodeMirror from '@uiw/react-codemirror';
import {python} from '@codemirror/lang-python';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';
import DOMPurify from 'dompurify';
import { GitMerge, ArrowDownUp, BookOpen, Folder, FolderInput, FolderPlus, LayoutGrid, List as ListIcon, PanelLeftClose, PanelLeftOpen, Plus, Play, Square, RefreshCcw, Save, Download, Upload, Trash2, ChevronUp, ChevronDown, Code2, FileText, Cpu, LogOut, Users, Shield, X, Settings2, RotateCcw, CircleHelp, Activity, Check, AlertCircle, NotebookPen, Eye, Pencil, CircleStop, Zap, Search, Copy } from 'lucide-react';
import './style.css';
import {applyNotebookDelta,mergeRemoteOutput} from './liveEvents.mjs';

// Markdown rendering: GFM tables/lists + KaTeX math ($...$ inline, $$...$$ display).
// strict:'ignore' keeps KaTeX from throwing on unicode inside math (e.g. \text{中文}),
// and throwOnError:false + errorColor means a broken formula degrades to red source text
// instead of breaking the whole cell.
const katexOptions = {throwOnError:false, strict:'ignore', errorColor:'#e06c75', trust:false};
const mdPlugins = {remarkPlugins:[remarkGfm, remarkMath], rehypePlugins:[[rehypeKatex, katexOptions]]};
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
    const error = new Error(typeof message === 'string' ? message : JSON.stringify(message));
    error.status = res.status;
    throw error;
  }
  if ((res.headers.get('content-type') || '').includes('application/json')) return res.json();
  return res;
}
function formatSize(bytes) {
  if (!bytes) return '—';
  const units = ['B','KB','MB','GB'];
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}
function sourceText(src) { return Array.isArray(src) ? src.join('') : (src || ''); }
function nbOutput(kind, content) {
  if (kind === 'stream') return {output_type:'stream', name:content.name || 'stdout', text:sourceText(content.text)};
  if (kind === 'error') return {output_type:'error', ename:content.ename || 'Error', evalue:content.evalue || '', traceback:content.traceback || []};
  const out = {output_type:kind === 'execute_result' ? 'execute_result' : 'display_data', data:content.data || {}, metadata:content.metadata || {}};
  if (kind === 'execute_result') out.execution_count = content.execution_count ?? null;
  return out;
}
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

const SORT_OPTIONS = [
  {value:'updated-desc', label:'Recently updated'},
  {value:'updated-asc',  label:'Oldest first'},
  {value:'name-asc',     label:'Name A → Z'},
  {value:'name-desc',    label:'Name Z → A'},
  {value:'size-desc',    label:'Largest first'},
  {value:'size-asc',     label:'Smallest first'},
];

// Folders carry `name`, notebooks carry `title` — normalise so one comparator
// handles both lists (sorting by "name" must not blow up on notebooks).
function displayName(item) { return (item.name || item.title || '').toLowerCase(); }
function sortItems(list, sort) {
  const [key, dir] = sort.split('-');
  const sign = dir === 'asc' ? 1 : -1;
  return [...list].sort((a, b) => {
    if (key === 'name') return sign * displayName(a).localeCompare(displayName(b));
    if (key === 'size') return sign * ((a.size_bytes || 0) - (b.size_bytes || 0));
    const at = a.updated_at ? Date.parse(a.updated_at) : 0;
    const bt = b.updated_at ? Date.parse(b.updated_at) : 0;
    return sign * (at - bt);
  });
}

function folderView(folder, sizeByFolder) {
  return {id:folder.id, name:folder.name, updated_at:folder.updated_at,
          notebook_count:folder.notebook_count, size_bytes:sizeByFolder[folder.id] || 0};
}

function FolderCard({folder, onOpen, onRename, onDelete}) {
  return <div className="nb-card folder-card" data-kind="folder">
    <button className="nb-card-main" onClick={()=>onOpen(folder.id)} title={`Open ${folder.name}`} aria-label={`Open folder ${folder.name}`}>
      <div className="nb-card-icon folder"><Folder size={23}/></div>
      <h3>{folder.name}</h3>
      <p>{folder.notebook_count} notebook{folder.notebook_count===1?'':'s'} · {formatSize(folder.size_bytes)}</p>
      <div className="card-foot"><span className="role">folder</span><small>{folder.updated_at?new Date(folder.updated_at).toLocaleDateString():''}</small></div>
    </button>
    <div className="card-actions">
      <button title="Rename folder" aria-label={`Rename folder ${folder.name}`} onClick={()=>onRename(folder)}><Pencil size={13}/></button>
      <button title="Delete folder (notebooks return to the root)" aria-label={`Delete folder ${folder.name}`} onClick={()=>onDelete(folder)}><Trash2 size={13}/></button>
    </div>
  </div>;
}

function FolderRow({folder, onOpen, onRename, onDelete}) {
  return <div className="item-row folder" data-kind="folder">
    <button className="item-main" onClick={()=>onOpen(folder.id)} aria-label={`Open folder ${folder.name}`}>
      <span className="item-icon folder"><Folder size={17}/></span>
      <span className="item-name">{folder.name}</span>
      <span className="item-meta">{folder.notebook_count} notebook{folder.notebook_count===1?'':'s'}</span>
      <span className="item-size">{formatSize(folder.size_bytes)}</span>
      <span className="item-date">{folder.updated_at?new Date(folder.updated_at).toLocaleDateString():'—'}</span>
    </button>
    <span className="item-actions">
      <button title="Rename folder" aria-label={`Rename folder ${folder.name}`} onClick={()=>onRename(folder)}><Pencil size={13}/></button>
      <button title="Delete folder" aria-label={`Delete folder ${folder.name}`} onClick={()=>onDelete(folder)}><Trash2 size={13}/></button>
    </span>
  </div>;
}

function NotebookRow({item, onOpen, onMove}) {
  return <div className="item-row" data-kind="notebook">
    <button className="item-main" onClick={()=>onOpen(item.id)} aria-label={`Open notebook ${item.title}`}>
      <span className="item-icon"><Code2 size={17}/></span>
      <span className="item-name">{item.title}</span>
      <span className="item-meta">{item.cells ?? '—'} cell{item.cells===1?'':'s'} <span className={'role '+item.role}>{item.role}</span></span>
      <span className="item-size">{formatSize(item.size_bytes)}</span>
      <span className="item-date">{item.updated_at?new Date(item.updated_at).toLocaleDateString():'—'}</span>
    </button>
    <span className="item-actions">
      {onMove&&<button title="Move to folder" aria-label={`Move notebook ${item.title}`} onClick={()=>onMove(item)}><FolderInput size={13}/></button>}
    </span>
  </div>;
}

function MoveDialog({item, folders, onClose, onMove}) {
  return <Modal title="Move notebook" onClose={onClose}><div className="modal-body">
    <p className="muted">Choose a folder for <strong>{item.title}</strong>. Folders only organise your own list; sharing stays per notebook.</p>
    <div className="scroll-list">
      <div className="list-line"><span><BookOpen size={14} style={{verticalAlign:'middle',marginRight:7}}/>My notebooks (root)</span>
        <button className="small-link" disabled={!item.folder_id} onClick={()=>onMove(null)}>{item.folder_id?'Move here':'Current'}</button></div>
      {folders.map(f=><div className="list-line" key={f.id}><span><Folder size={14} style={{verticalAlign:'middle',marginRight:7}}/>{f.name}<small> · {f.notebook_count}</small></span>
        <button className="small-link" disabled={item.folder_id===f.id} onClick={()=>onMove(f.id)}>{item.folder_id===f.id?'Current':'Move here'}</button></div>)}
    </div>
    {folders.length===0&&<p className="muted">No folders yet — create one from the dashboard first.</p>}
  </div></Modal>;
}

function Dashboard({user, open, openFolder, goHome, openShared, folderId, sharedOnly=false, logout}) {
 const [items,setItems] = useState([]); const [folders,setFolders]=useState([]); const [foldersLoaded,setFoldersLoaded]=useState(false);
 const [error,setError]=useState(''); const [search,setSearch]=useState('');
 const [newTitle,setNewTitle]=useState('Untitled notebook'); const [createOpen,setCreateOpen]=useState(false);
 const [folderOpen,setFolderOpen]=useState(false); const [newFolderName,setNewFolderName]=useState(''); const [renaming,setRenaming]=useState(null);
 const [moveTarget,setMoveTarget]=useState(null);
 const [view,setView]=useState(()=>localStorage.getItem('nebula:view')||'grid');
 const [sort,setSort]=useState(()=>localStorage.getItem('nebula:sort')||'updated-desc');
 const [navCollapsed,setNavCollapsed]=useState(()=>localStorage.getItem('nebula:nav-collapsed')==='1');
 const [adminOpen,setAdminOpen]=useState(false);
 const [users,setUsers]=useState([]);const [newUser,setNewUser]=useState({email:'',password:'',is_admin:false});const [audit,setAudit]=useState([]);
 const fileRef=useRef();
 useEffect(()=>{localStorage.setItem('nebula:view',view)},[view]);
 useEffect(()=>{localStorage.setItem('nebula:sort',sort)},[sort]);
 useEffect(()=>{localStorage.setItem('nebula:nav-collapsed',navCollapsed?'1':'0')},[navCollapsed]);
 const refresh=useCallback(async()=>{
   try{const [nbs,fls]=await Promise.all([api('/notebooks'),api('/folders')]);setItems(nbs);setFolders(fls);setFoldersLoaded(true)}
   catch(e){setError(e.message)}},[]);
 // Keep the dashboard current when an MCP agent creates or moves notebooks.
 useEffect(()=>{
   let timer,closed=false;
   const sync=async()=>{if(closed)return;
     if(!document.hidden)await refresh();
     if(!closed)timer=setTimeout(sync,document.hidden?20000:5000);
   };
   timer=setTimeout(sync,5000);
   return()=>{closed=true;clearTimeout(timer)};
 },[refresh]);
 // Fetch once per mount: navigating between folders must not reset/refetch the
 // whole dashboard (that caused a flash of an empty page).
 useEffect(()=>{refresh()},[refresh]);
 // Deep links: only bounce home once the folder list is known and the id is absent.
 useEffect(()=>{
   if(folderId&&foldersLoaded&&!folders.some(f=>f.id===folderId)){goHome()}
 },[folderId,folders,foldersLoaded,goHome]);
 const activeFolder = !sharedOnly ? folders.find(f=>f.id===folderId)||null : null;
 const sizeByFolder = {};
 for(const n of items){ if(n.folder_id) sizeByFolder[n.folder_id]=(sizeByFolder[n.folder_id]||0)+(n.size_bytes||0); }
 async function create(){try{const n=await api('/notebooks',{method:'POST',body:JSON.stringify({title:newTitle,folder_id:folderId})}); setCreateOpen(false);open(n.id)}catch(e){setError(e.message)}}
 async function createFolder(e){e.preventDefault();try{await api('/folders',{method:'POST',body:JSON.stringify({name:newFolderName})});setNewFolderName('');setFolderOpen(false);await refresh()}catch(e){setError(e.message)}}
 async function renameFolder(e){e.preventDefault();try{await api('/folders/'+renaming.id,{method:'PATCH',body:JSON.stringify({name:renaming.name})});setRenaming(null);await refresh()}catch(e){setError(e.message)}}
 async function deleteFolder(f){if(!confirm(`Delete the folder “${f.name}”? The ${f.notebook_count} notebook(s) inside are kept and moved back to the root.`))return;try{await api('/folders/'+f.id,{method:'DELETE'});if(folderId===f.id)goHome();await refresh()}catch(e){setError(e.message)}}
 async function moveNotebook(item,folder){try{await api(`/notebooks/${item.id}/folder`,{method:'PUT',body:JSON.stringify({folder_id:folder})});setMoveTarget(null);await refresh()}catch(e){setError(e.message)}}
 async function importFile(file){if(!file)return;const data=new FormData();data.append('file',file);if(folderId)data.append('folder_id',folderId);try{const n=await api('/notebooks/import',{method:'POST',body:data});open(n.id)}catch(e){setError(e.message)}finally{if(fileRef.current)fileRef.current.value=''}}
 async function loadAdmin(){try{setUsers(await api('/admin/users'));setAudit(await api('/admin/audit'));setAdminOpen(true)}catch(e){setError(e.message)}}
 async function createUser(e){e.preventDefault();try{await api('/admin/users',{method:'POST',body:JSON.stringify(newUser)});setNewUser({email:'',password:'',is_admin:false});setUsers(await api('/admin/users'))}catch(e){setError(e.message)}}
 async function toggleUser(u){try{await api('/admin/users/'+u.id,{method:'PATCH',body:JSON.stringify({is_active:!u.is_active})});setUsers(await api('/admin/users'))}catch(e){setError(e.message)}}
 const term = search.trim().toLowerCase();
 const visibleFolders = (sharedOnly?[]:folders)
   .map(f=>folderView(f,sizeByFolder))
   .filter(f=>!term||f.name.toLowerCase().includes(term));
 const notebooksInScope = items.filter(n=>sharedOnly?n.is_shared:(n.is_shared?false:(folderId?n.folder_id===folderId:n.folder_id==null)))
   .filter(n=>!term||n.title.toLowerCase().includes(term));
 const sortedFolders = sortItems(visibleFolders, sort);
 const sortedNotebooks = sortItems(notebooksInScope, sort);
 const rootCount = items.filter(n=>!n.is_shared&&n.folder_id==null).length;
 const sharedCount = items.filter(n=>n.is_shared).length;
 const subtitle = sharedOnly ? 'Notebooks shared with your account; fork a saved version to work privately.' : activeFolder
   ? `${activeFolder.notebook_count} notebook${activeFolder.notebook_count===1?'':'s'} · ${formatSize(sizeByFolder[activeFolder.id]||0)}`
   : 'Create, experiment, and share interactive Python notebooks.';

 const folderGrid = <div className="notebook-grid folder-grid">{sortedFolders.map(f=><FolderCard key={f.id} folder={f} onOpen={openFolder} onRename={setRenaming} onDelete={deleteFolder}/>)}</div>;
 const folderList = <div className="item-list">{sortedFolders.map(f=><FolderRow key={f.id} folder={f} onOpen={openFolder} onRename={setRenaming} onDelete={deleteFolder}/>)}</div>;
 const notebookGrid = <div className="notebook-grid">{sortedNotebooks.map(n=><div key={n.id} className="nb-card" data-kind="notebook"><button className="nb-card-main" onClick={()=>open(n.id)} aria-label={`Open notebook ${n.title}`}><div className="nb-card-icon"><Code2 size={23}/></div><h3>{n.title}</h3><p>{n.cells ?? '—'} cells · {formatSize(n.size_bytes)}</p><div className="card-foot"><span className={'role '+n.role}>{n.role}</span><small>{n.updated_at?new Date(n.updated_at).toLocaleDateString():''}</small></div></button>{!n.is_shared&&<div className="card-actions"><button title="Move to folder" aria-label={`Move notebook ${n.title}`} onClick={()=>setMoveTarget(n)}><FolderInput size={13}/></button></div>}</div>)}</div>;
 const notebookList = <div className="item-list">{sortedNotebooks.map(n=><NotebookRow key={n.id} item={n} onOpen={open} onMove={n.is_shared?null:setMoveTarget}/>)}</div>;

 return <div className={'app-shell'+(navCollapsed?' nav-collapsed':'')}>
  <aside className="sidebar">
   <div className="brand"><span className="logo"><NotebookPen size={21}/></span><span>Nebula <strong>Notebook</strong></span>
     <button className="icon-btn nav-toggle" title={navCollapsed?'Expand sidebar':'Collapse sidebar'} aria-label={navCollapsed?'Expand sidebar':'Collapse sidebar'} aria-expanded={!navCollapsed} onClick={()=>setNavCollapsed(v=>!v)}>{navCollapsed?<PanelLeftOpen size={17}/>:<PanelLeftClose size={17}/>}</button></div>
   <div className="nav-scroll">
    <button className={'nav-item'+(!sharedOnly&&folderId===null?' active':'')} title="My notebooks" onClick={goHome}><BookOpen size={18}/> <span className="nav-text">My notebooks</span> <small className="nav-count">{rootCount}</small></button>
    <button className={'nav-item'+(sharedOnly?' active':'')} title="Shared with me" onClick={openShared}><Users size={18}/> <span className="nav-text">Shared with me</span> <small className="nav-count">{sharedCount}</small></button>
    <div className="nav-group"><small className="nav-label">FOLDERS</small>
      {folders.map(f=><button key={f.id} className={'nav-item'+(folderId===f.id?' active':'')} title={f.name} onClick={()=>openFolder(f.id)}><Folder size={17}/> <span className="nav-text">{f.name}</span> <small className="nav-count">{f.notebook_count}</small></button>)}
      {folders.length===0&&<small className="nav-empty">No folders yet</small>}
      <button className="nav-item" title="New folder" onClick={()=>{setNewFolderName('');setFolderOpen(true)}}><FolderPlus size={17}/> <span className="nav-text">New folder</span></button>
    </div>
   </div>
   <div className="nav-bottom"><div className="user-small"><div className="avatar">{user.email[0].toUpperCase()}</div><div><strong>{user.email.split('@')[0]}</strong><small>{user.is_admin?'Administrator':'Member'}</small></div></div>{user.is_admin&&<button className="nav-item" onClick={loadAdmin} title="Administration"><Shield size={18}/> <span className="nav-text">Administration</span></button>}<button className="nav-item" onClick={logout} title="Sign out"><LogOut size={18}/> <span className="nav-text">Sign out</span></button></div>
  </aside>
  <main className="dashboard">
   <div className="dash-top"><div><small className="eyebrow">WORKSPACE</small><h1>{sharedOnly?'Shared with me':activeFolder?activeFolder.name:'Your notebooks'}<span className="dot">.</span></h1><p className="muted">{subtitle}</p></div>
   <div className="top-actions"><HeaderButton onClick={()=>fileRef.current?.click()}><Upload size={16}/> Import .ipynb</HeaderButton><HeaderButton onClick={()=>{setNewFolderName('');setFolderOpen(true)}}><FolderPlus size={16}/> New folder</HeaderButton><HeaderButton className="primary" onClick={()=>setCreateOpen(true)}><Plus size={17}/> New notebook</HeaderButton></div></div>
   <nav className="breadcrumb" aria-label="Breadcrumb"><button onClick={goHome} className={activeFolder||sharedOnly?'':'current'} aria-current={activeFolder||sharedOnly?undefined:'page'}>My notebooks</button>{sharedOnly&&<><span className="sep">/</span><span className="current" aria-current="page">Shared with me</span></>}{activeFolder&&<><span className="sep">/</span><span className="current" aria-current="page">{activeFolder.name}</span></>}</nav>
   {error&&<div className="alert"><AlertCircle size={16}/>{error}<button onClick={()=>setError('')}>×</button></div>}
   <input type="file" accept=".ipynb,application/json" style={{display:'none'}} ref={fileRef} onChange={e=>importFile(e.target.files?.[0])}/>
   <div className="section-heading"><h2>{sharedOnly?'Shared notebooks':activeFolder?'In this folder':'Recent notebooks'} <span>{sortedNotebooks.length + (folderId===null?sortedFolders.length:0)}</span></h2>
     <div className="list-controls">
       <div className="search"><Search size={15}/><input placeholder="Search" value={search} onChange={e=>setSearch(e.target.value)}/></div>
       <label className="sort-control"><ArrowDownUp size={14}/><select value={sort} onChange={e=>setSort(e.target.value)} aria-label="Sort by">{SORT_OPTIONS.map(o=><option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
       <div className="view-toggle" role="group" aria-label="Layout">
         <button className={view==='grid'?'active':''} title="Grid view" aria-label="Grid view" aria-pressed={view==='grid'} onClick={()=>setView('grid')}><LayoutGrid size={16}/></button>
         <button className={view==='list'?'active':''} title="List view" aria-label="List view" aria-pressed={view==='list'} onClick={()=>setView('list')}><ListIcon size={16}/></button>
       </div>
     </div>
   </div>
   {view==='list'&&(sortedFolders.length>0||sortedNotebooks.length>0)&&<div className="item-head" aria-hidden="true"><span className="item-icon"/><span className="item-name">Name</span><span className="item-meta">Contents</span><span className="item-size">Size</span><span className="item-date">Updated</span><span className="item-actions"/></div>}
   {!sharedOnly&&folderId===null&&sortedFolders.length>0&&(view==='grid'?folderGrid:folderList)}
   {view==='grid'?notebookGrid:notebookList}
   {sortedFolders.length===0&&sortedNotebooks.length===0&&<p className="muted">Nothing here yet — create a notebook or a folder to get started.</p>}
  </main>
  {createOpen&&<Modal title={activeFolder?`New notebook in “${activeFolder.name}”`:'Create notebook'} onClose={()=>setCreateOpen(false)}><div className="modal-body"><label>Notebook name<input value={newTitle} onChange={e=>setNewTitle(e.target.value)} onKeyDown={e=>{if(e.key==='Enter')create()}} autoFocus/></label><div className="modal-actions"><HeaderButton onClick={()=>setCreateOpen(false)}>Cancel</HeaderButton><HeaderButton className="primary" onClick={create}>Create notebook</HeaderButton></div></div></Modal>}
  {folderOpen&&<Modal title="New folder" onClose={()=>setFolderOpen(false)}><form className="modal-body" onSubmit={createFolder}><label>Folder name<input value={newFolderName} onChange={e=>setNewFolderName(e.target.value)} placeholder="e.g. sklearn-tutorials" autoFocus/></label><p className="muted">Folders group your own notebooks and get their own URL (/folder/…). You can move notebooks in and out at any time, and deleting a folder never deletes its notebooks.</p><div className="modal-actions"><HeaderButton onClick={()=>setFolderOpen(false)}>Cancel</HeaderButton><HeaderButton className="primary" type="submit">Create folder</HeaderButton></div></form></Modal>}
  {renaming&&<Modal title="Rename folder" onClose={()=>setRenaming(null)}><form className="modal-body" onSubmit={renameFolder}><label>Folder name<input value={renaming.name} onChange={e=>setRenaming({...renaming,name:e.target.value})} autoFocus/></label><div className="modal-actions"><HeaderButton onClick={()=>setRenaming(null)}>Cancel</HeaderButton><HeaderButton className="primary" type="submit">Save name</HeaderButton></div></form></Modal>}
  {moveTarget&&<MoveDialog item={moveTarget} folders={folders} onClose={()=>setMoveTarget(null)} onMove={f=>moveNotebook(moveTarget,f)}/>}
  {adminOpen&&<Modal title="User administration" onClose={()=>setAdminOpen(false)}><div className="modal-body"><h4>Create user</h4><form className="admin-form" onSubmit={createUser}><input type="email" placeholder="Email" value={newUser.email} onChange={e=>setNewUser({...newUser,email:e.target.value})} required/><input type="password" minLength="12" placeholder="Password (12+ chars)" value={newUser.password} onChange={e=>setNewUser({...newUser,password:e.target.value})} required/><label className="inline-check"><input type="checkbox" checked={newUser.is_admin} onChange={e=>setNewUser({...newUser,is_admin:e.target.checked})}/> Admin</label><HeaderButton className="primary" type="submit">Create user</HeaderButton></form><h4>Users</h4><div className="scroll-list">{users.map(u=><div className="list-line" key={u.id}><span>{u.email}<small>{u.is_admin?' · admin':''}</small></span><button className="small-link" disabled={u.id===user.id} onClick={()=>toggleUser(u)}>{u.is_active?'Deactivate':'Activate'}</button></div>)}</div><h4>Latest audit events</h4><div className="scroll-list">{audit.slice(0,15).map((a,i)=><div className="list-line" key={i}><span>{a.event}<small> · {a.target_id.slice(0,8)}</small></span><small>{new Date(a.created_at).toLocaleString()}</small></div>)}</div></div></Modal>}
 </div>;
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
 <div className="markdown-area">{canEdit&&markdownEdit?<textarea rows={Math.max(3,sourceText(cell.source).split('\n').length+1)} value={sourceText(cell.source)} onChange={e=>update(index,{source:e.target.value})} onBlur={()=>setMarkdownEdit(false)} placeholder="Write Markdown here..."/>:<div className="markdown-preview" onDoubleClick={()=>canEdit&&setMarkdownEdit(true)}><ReactMarkdown {...mdPlugins}>{sourceText(cell.source)||'*Double-click to edit Markdown*'}</ReactMarkdown>{canEdit&&<button className="edit-markdown" onClick={()=>setMarkdownEdit(true)}><Pencil size={14}/> Edit</button>}</div>}</div>}
 {cell.cell_type==='code'&&(cell.outputs||[]).length>0&&<div className="output-area">{cell.outputs.map((o,i)=><RichOutput output={o} key={i}/>)}</div>}
 </div></div>;
}

function ShareDialog({id,onClose}) {
 const [shares,setShares]=useState([]),[email,setEmail]=useState(''),[role,setRole]=useState('viewer'),[error,setError]=useState('');
 const load=useCallback(async()=>{try{setShares(await api(`/notebooks/${id}/shares`))}catch(e){setError(e.message)}},[id]);
 useEffect(()=>{load()},[load]);
 async function share(){try{await api(`/notebooks/${id}/shares`,{method:'PUT',body:JSON.stringify({email,role})});setEmail('');await load()}catch(e){setError(e.message)}}
 async function revoke(uid){try{await api(`/notebooks/${id}/shares/${uid}`,{method:'DELETE'});await load()}catch(e){setError(e.message)}}
 return <Modal title="Share notebook" onClose={onClose}><div className="modal-body"><p className="muted">Share with an existing Nebula account by email (create the account in Administration first). Viewers can read saved content; editors can edit and execute Python in this notebook’s runtime.</p><div className="share-form"><input type="email" placeholder="Registered user's email" value={email} onChange={e=>setEmail(e.target.value)}/><select value={role} onChange={e=>setRole(e.target.value)}><option value="viewer">Viewer</option><option value="editor">Editor</option></select><HeaderButton className="primary" onClick={share}>Share</HeaderButton></div>{error&&<div className="alert">{error}</div>}<h4>People with access</h4>{shares.length===0&&<p className="muted">Not shared with anyone.</p>}{shares.map(s=><div className="list-line" key={s.user_id}><span>{s.email} <small>· {s.role}</small></span><button className="small-link" onClick={()=>revoke(s.user_id)}>Revoke</button></div>)}</div></Modal>;
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
 const dirtyRef=useRef(false),lastExternalVersion=useRef(0);
 const baselineRef=useRef(null),baselineTitleRef=useRef('');
 const mergeBlockedRef=useRef(false),mergeOperationRef=useRef(null),mergePreviewRevisionRef=useRef(null),draftForkOpRef=useRef(null);
 const [mergeOpen,setMergeOpen]=useState(false),[mergeBusy,setMergeBusy]=useState(false),
       [mergePreview,setMergePreview]=useState(null),[mergeTheirs,setMergeTheirs]=useState(null),
       [mergeChoices,setMergeChoices]=useState({}),[mergeError,setMergeError]=useState('');
 const eventCursorRef=useRef(0),syncedCursorRef=useRef(0),eventStreamRef=useRef(null),liveRef=useRef(false);
 const [liveConnected,setLiveConnected]=useState(false);
 const canEdit = !!entry && entry.role!=='viewer';
 const [folderCrumb,setFolderCrumb]=useState(null);
 const [historyOpen,setHistoryOpen]=useState(false),[historyRows,setHistoryRows]=useState([]),
       [selectedVersion,setSelectedVersion]=useState(''),[forking,setForking]=useState(false),
       [hasOlderHistory,setHasOlderHistory]=useState(false),[historyLoading,setHistoryLoading]=useState(false);
 function editDoc(fn){setDoc(prev=>{const next=typeof fn==='function'?fn(prev):fn;latestDoc.current=next;return next});revisions.current++;dirtyRef.current=true;setRevision(revisions.current);setDirty(true)}
 function updateCell(index,patch){editDoc(d=>({...d,cells:d.cells.map((c,i)=>i===index?{...c,...patch}:c)}))}
 const fetchRuntime=useCallback(async()=>{try{setRuntime(await api(`/notebooks/${id}/runtime`))}catch{}},[id]);
 useEffect(()=>{let active=true; (async()=>{try{const n=await api(`/notebooks/${id}`);if(!active)return;setEntry(n);setDoc(normalize(n.document));latestDoc.current=normalize(n.document);baselineRef.current={version:n.version,document:normalize(n.document)};baselineTitleRef.current=n.title;setTitle(n.title);latestTitle.current=n.title;versions.current=n.version;eventCursorRef.current=n.event_cursor||0;syncedCursorRef.current=n.event_cursor||0;setRuntime(n.runtime||{mode:null,status:'stopped'});if(n.runtime?.status==='ready')connectChannels();setCaps(await api('/runtime/capabilities'));
   if(n.folder_id){try{const folders=await api('/folders');const f=folders.find(x=>x.id===n.folder_id);if(active&&f)setFolderCrumb({id:f.id,name:f.name})}catch{}}
 }catch(e){if(active)setError(e.message)}})();return()=>{active=false;socket.current?.close()}},[id]);
 const save=useCallback(async()=>{if(!canEdit||savingRef.current||!dirty||!latestDoc.current||mergeBlockedRef.current)return;const at=revisions.current;const snapshot=latestDoc.current;const name=latestTitle.current; savingRef.current=true;setSaving('saving');try{const result=await api(`/notebooks/${id}`,{method:'PUT',body:JSON.stringify({document:snapshot,title:name,version:versions.current})});versions.current=result.version;baselineRef.current={version:result.version,document:normalize(snapshot)};baselineTitleRef.current=name;if(revisions.current===at){dirtyRef.current=false;setDirty(false);setSaving('saved')}else{setSaving('unsaved')}}catch(e){if(e.status===409){mergeBlockedRef.current=true;setMergeChoices({});mergeOperationRef.current=null;setMergePreview(null);setMergeOpen(true);setNotice('Another collaborator saved changes. Resolve the draft before saving.');}else setError(e.message);setSaving('error')}finally{savingRef.current=false;if(revisions.current!==at&&!mergeBlockedRef.current)setSaveTick(x=>x+1)}},[canEdit,dirty,id]);
 useEffect(()=>{if(!dirty||!canEdit)return;const t=setTimeout(save,1100);return()=>clearTimeout(t)},[revision,saveTick,dirty,save,canEdit]);
 // Version-only polling: no large document transfer on unchanged revisions.
 // Never replace unsaved or in-flight browser edits with an external MCP write.
 useEffect(()=>{
   if(!entry)return;
   let closed=false,timer=null;
   async function syncExternal(){
     try{
       // SSE is primary when healthy; version probe is only a conservative fallback.
       if(liveRef.current&&!document.hidden)return;
       const revision=await api(`/notebooks/${id}/revision`);
       if(!closed&&revision.version>versions.current){
         if(dirtyRef.current||savingRef.current){
           if(lastExternalVersion.current!==revision.version){
             lastExternalVersion.current=revision.version;
             setNotice('Notebook changed in another client. Save or resolve your edits before reloading.');
           }
         }else{
           const current=await api(`/notebooks/${id}`);
           if(!closed&&current.version>versions.current&&!dirtyRef.current&&!savingRef.current){
             const newDoc=normalize(current.document);
             versions.current=current.version;baselineRef.current={version:current.version,document:newDoc};baselineTitleRef.current=current.title;
             latestDoc.current=newDoc;setDoc(newDoc);
             latestTitle.current=current.title;setTitle(current.title);
             setEntry(current);
             setSaving('saved');
             lastExternalVersion.current=0;
             setNotice('Notebook updated from another client');
           }
         }
       }
     }catch(e){if(!closed&&e?.message?.includes('403'))setNotice('Notebook access changed; reload to verify permissions.')}
     finally{if(!closed)timer=setTimeout(syncExternal,document.hidden?30000:8000)}
   }
   timer=setTimeout(syncExternal,8000);
   const onVisibility=()=>{if(!document.hidden){clearTimeout(timer);timer=setTimeout(syncExternal,1000)}};
   document.addEventListener('visibilitychange',onVisibility);
   return()=>{closed=true;clearTimeout(timer);document.removeEventListener('visibilitychange',onVisibility)};
 },[id,entry?.id]);

 // One SSE reader per visible Notebook. Kernel status/output uses the durable
 // journal; source edits apply small Cell deltas only when the base version is exact.
 // On reconnect EventSource uses Last-Event-ID; on foreground re-open use last cursor.
 useEffect(()=>{
   if(!entry?.id)return;
   let closed=false,source=null,chain=Promise.resolve(),requestId=0;
   async function reloadLatest(){
     const n=await api(`/notebooks/${id}`);
     if(closed||dirtyRef.current||savingRef.current||n.version<versions.current)return;
     const next=normalize(n.document);
     versions.current=n.version;baselineRef.current={version:n.version,document:next};baselineTitleRef.current=n.title;
     eventCursorRef.current=Math.max(eventCursorRef.current,n.event_cursor||0);
     syncedCursorRef.current=Math.max(syncedCursorRef.current,n.event_cursor||0);
     latestDoc.current=next;setDoc(next);latestTitle.current=n.title;setTitle(n.title);
     setEntry(n);setSaving('saved');
   }
   function applyPatch(event){
     const payload=event.payload||{};
     const version=event.document_version;
     if(!version||version<=versions.current)return Promise.resolve();
     if(dirtyRef.current||savingRef.current){
       if(lastExternalVersion.current!==version){
         lastExternalVersion.current=version;
         setNotice('A collaborator changed this notebook. Resolve your unsaved edits before reloading.');
       }
       return Promise.resolve();
     }
     const updated=applyNotebookDelta(latestDoc.current,payload,versions.current,version);
     if(!updated)return reloadLatest();
     latestDoc.current=updated;setDoc(updated);versions.current=version;
     baselineRef.current={version,document:updated};
     if(typeof payload.title==='string'){baselineTitleRef.current=payload.title;}
     if(typeof payload.title==='string'){latestTitle.current=payload.title;setTitle(payload.title);}
     setEntry(previous=>previous?{...previous,version,title:payload.title||previous.title}:previous);
     lastExternalVersion.current=0;
     return Promise.resolve();
   }
   async function handleEvent(event){
     eventCursorRef.current=Math.max(eventCursorRef.current,event.event_id||0);
     // An authoritative reload already includes these saved outputs/changes.
     if((event.event_id||0)<=syncedCursorRef.current)return;
     if(event.kind==='document.committed')return applyPatch(event);
     const cellId=event.cell_id;
     if(!cellId)return;
     if(event.kind==='run.queued'||event.kind==='run.started'||event.kind==='run.cancelling'||event.kind==='run.output'){
       setRunningIds(previous=>({...previous,[cellId]:true}));
     }else if(event.kind==='run.finished'){
       setRunningIds(previous=>{const copy={...previous};delete copy[cellId];return copy;});
     }
     if(event.kind==='run.output'&&!dirtyRef.current&&!savingRef.current){
       const outputs=event.payload?.outputs;
       if(!Array.isArray(outputs)||outputs.length>100)return;
       const updated=mergeRemoteOutput(latestDoc.current,cellId,outputs);
       if(updated){latestDoc.current=updated;setDoc(updated);}
     }
   }
   function disconnect(){
     if(source){source.close();source=null;}
     eventStreamRef.current=null;liveRef.current=false;setLiveConnected(false);
   }
   function connect(){
     disconnect();
     if(closed||document.hidden)return;
     const thisId=++requestId;
     source=new EventSource(`/api/notebooks/${encodeURIComponent(id)}/events?after=${eventCursorRef.current}`);
     eventStreamRef.current=source;
     source.onopen=()=>{if(closed||thisId!==requestId)return;liveRef.current=true;setLiveConnected(true)};
     source.addEventListener('notebook',message=>{
       if(closed||thisId!==requestId)return;
       try{
         const event=JSON.parse(message.data);
         chain=chain.then(()=>handleEvent(event)).catch(()=>reloadLatest().catch(()=>{}));
       }catch{chain=chain.then(()=>reloadLatest()).catch(()=>{})}
     });
     source.addEventListener('access_revoked',()=>{
       setNotice('Notebook access was revoked or your session expired.');
       disconnect();
     });
     source.onerror=()=>{liveRef.current=false;setLiveConnected(false)};
   }
   const visibility=()=>{
     if(document.hidden)disconnect();
     else{connect();if(!dirtyRef.current&&!savingRef.current)reloadLatest().catch(()=>{})}
   };
   document.addEventListener('visibilitychange',visibility);
   connect();
   return()=>{closed=true;disconnect();document.removeEventListener('visibilitychange',visibility)};
 },[id,entry?.id]);

 useEffect(()=>{if(!dirty||!canEdit)return;const handler=e=>{e.preventDefault();e.returnValue='';};window.addEventListener('beforeunload',handler);return()=>window.removeEventListener('beforeunload',handler)},[dirty,canEdit]);
 function changeTitle(t){setTitle(t);latestTitle.current=t;revisions.current++;dirtyRef.current=true;setRevision(revisions.current);setDirty(true)}
 function move(i,delta){editDoc(d=>{const arr=[...d.cells];const j=i+delta;if(j<0||j>=arr.length)return d;[arr[i],arr[j]]=[arr[j],arr[i]];return {...d,cells:arr}})}
 function add(index,type){editDoc(d=>({...d,cells:[...d.cells.slice(0,index),newCell(type),...d.cells.slice(index)]}))}
 function remove(index){editDoc(d=>({...d,cells:d.cells.filter((_,i)=>i!==index)}))}
 function receiveMessage(raw){let m;try{m=JSON.parse(raw)}catch{return}const kind=m.header?.msg_type, req=m.parent_header?.msg_id;const cellId=pending.current.get(req);if(!cellId)return;
   if(kind==='status'&&m.content?.execution_state==='idle'){pending.current.delete(req);setRunningIds(s=>{const x={...s};delete x[cellId];return x});return}
   if(kind==='status'||kind==='execute_reply')return;
   editDoc(d=>({...d,cells:d.cells.map(c=>{if(c.id!==cellId)return c;let changes={};if(kind==='execute_input')changes.execution_count=m.content?.execution_count;
      if(['stream','execute_result','display_data','error'].includes(kind)){changes.outputs=[...(c.outputs||[]), nbOutput(kind, m.content || {})];}return {...c,...changes}})}));
 }
 function connectChannels(){setChannelReady(false);const proto=location.protocol==='https:'?'wss:':'ws:';const w=new WebSocket(`${proto}//${location.host}/api/notebooks/${id}/channels`);w.onopen=()=>setChannelReady(true);w.onmessage=e=>receiveMessage(e.data);w.onerror=()=>setNotice('Kernel WebSocket error — check connection.');w.onclose=()=>{if(socket.current===w){socket.current=null;setChannelReady(false);setRunningIds({});}};socket.current=w;return w;}
 async function start(){setBusy(true);setError('');try{if(choice==='gpu'&&!caps?.gpu_selectable)throw new Error('GPU unavailable on this server');const r=await api(`/notebooks/${id}/runtime`,{method:'POST',body:JSON.stringify({mode:choice})});setRuntime(r);socket.current?.close();connectChannels();setNotice(`${choice.toUpperCase()} runtime connected`)}catch(e){setError(e.message)}finally{setBusy(false)}}
 async function stop(){setBusy(true);try{socket.current?.close();await api(`/notebooks/${id}/runtime`,{method:'DELETE'});setRuntime({mode:null,status:'stopped'});pending.current.clear();setRunningIds({})}catch(e){setError(e.message)}finally{setBusy(false)}}
 async function interrupt(){try{await api(`/notebooks/${id}/runtime/interrupt`,{method:'POST'})}catch(e){setError(e.message)}}
 function run(cell){const ws=socket.current;if(!canEdit||runtime.status!=='ready'||!channelReady||!ws||ws.readyState!==WebSocket.OPEN){setError('Connect the runtime and wait until the Kernel channel is open');return}const msgId=randomId();pending.current.set(msgId,cell.id);setRunningIds(s=>({...s,[cell.id]:true}));editDoc(d=>({...d,cells:d.cells.map(c=>c.id===cell.id?{...c,outputs:[],execution_count:null}:c)}));
   ws.send(JSON.stringify({channel:'shell',header:{msg_id:msgId,username:'nebula',session:randomId(),date:new Date().toISOString(),msg_type:'execute_request',version:'5.3'},parent_header:{},metadata:{},content:{code:sourceText(cell.source),silent:false,store_history:true,user_expressions:{},allow_stdin:false,stop_on_error:true},buffers:[]}));
 }
 async function runAll(){if(!doc)return;for(const c of doc.cells){if(c.cell_type==='code')run(c)} }
 async function openHistory(){
   setHistoryOpen(true);setError('');
   try { const rows=await api(`/notebooks/${id}/versions?limit=100`);setHistoryRows(rows);setSelectedVersion(String(rows[0]?.version||''));setHasOlderHistory(rows.length===100&&rows[rows.length-1].version>1); }
   catch(e){setError(e.message)}
 }
 async function loadOlderVersions(){
   const oldest=historyRows[historyRows.length-1]?.version;
   if(!oldest||oldest<=1||historyLoading)return;
   setHistoryLoading(true);
   try{
     const older=await api(`/notebooks/${id}/versions?limit=100&before=${oldest}`);
     setHistoryRows(previous=>[...previous,...older]);
     setHasOlderHistory(older.length===100&&older[older.length-1].version>1);
   }catch(e){setError(e.message)}finally{setHistoryLoading(false)}
 }
 async function inspectMerge(choices=mergeChoices){
   const baseline=baselineRef.current;
   if(!baseline||!latestDoc.current)return;
   const snapshot=latestDoc.current,at=revisions.current;
   setMergeBusy(true);setMergeError('');
   try{
     const [plan,current]=await Promise.all([
       api(`/notebooks/${id}/merge/preview`,{method:'POST',
         body:JSON.stringify({base_version:baseline.version,mine_document:snapshot,
                               mine_title:latestTitle.current,resolutions:choices})}),
       api(`/notebooks/${id}`)
     ]);
     if(revisions.current!==at){setMergePreview(null);setMergeError('Draft changed during preview. Preview again before committing.');return;}
     mergePreviewRevisionRef.current=at;
     if(current.version!==plan.latest_version){
       setMergePreview(null);setMergeError('The shared Notebook changed during preview. Preview again.');
       return;
     }
     setMergePreview(plan);setMergeTheirs(current);
     if(mergeOperationRef.current?.latest_version!==plan.latest_version)
       mergeOperationRef.current=null;
   }catch(e){setMergeError(e.message)}
   finally{setMergeBusy(false)}
 }
 async function commitResolvedMerge(){
   if(!mergePreview?.can_merge||!baselineRef.current||!latestDoc.current)return;
   if(mergePreviewRevisionRef.current!==revisions.current){
     setMergePreview(null);setMergeError('Draft changed since preview; preview again.');return;
   }
   setMergeBusy(true);setMergeError('');
   const baseline=baselineRef.current;
   const original=mergeOperationRef.current;
   const operation=(original?.latest_version===mergePreview.latest_version&&
                    original?.revision===revisions.current&&
                    JSON.stringify(original.choices)===JSON.stringify(mergeChoices))
     ?original:{id:randomId(),latest_version:mergePreview.latest_version,
                revision:revisions.current,choices:{...mergeChoices}};
   mergeOperationRef.current=operation;
   try{
     const updated=await api(`/notebooks/${id}/merge/commit`,{method:'POST',
       body:JSON.stringify({base_version:baseline.version,
         expected_latest_version:operation.latest_version,operation_id:operation.id,
         mine_title:latestTitle.current,mine_document:latestDoc.current,
         resolutions:operation.choices})});
     const fresh=await api(`/notebooks/${id}`);
     const normalized=normalize(fresh.document);
     latestDoc.current=normalized;setDoc(normalized);
     latestTitle.current=fresh.title;setTitle(fresh.title);
     versions.current=fresh.version;
     baselineRef.current={version:fresh.version,document:normalized};
     baselineTitleRef.current=fresh.title;
     dirtyRef.current=false;setDirty(false);setSaving('saved');
     mergeBlockedRef.current=false;mergeOperationRef.current=null;
     setMergeOpen(false);setMergePreview(null);setMergeChoices({});
     setNotice('Collaboration conflict resolved and saved as version '+updated.version);
   }catch(e){
     if(e.status===409){
       setMergePreview(null);mergeOperationRef.current=null;
       setMergeError('Another edit arrived or a conflict remains. Refresh the preview; your draft is preserved.');
     }else{setMergeError(e.message)}
   }finally{setMergeBusy(false)}
 }
 async function forkMyUnsavedDraft(){
   const baseline=baselineRef.current;
   if(!baseline||!latestDoc.current)return;
   setMergeBusy(true);setMergeError('');
   const previous=draftForkOpRef.current;
   const op=previous?.revision===revisions.current?previous:{id:randomId(),revision:revisions.current};
   draftForkOpRef.current=op;
   try{
     const created=await api(`/notebooks/${id}/merge/fork-draft`,{method:'POST',
       body:JSON.stringify({base_version:baseline.version,mine_title:latestTitle.current,
                             mine_document:latestDoc.current,operation_id:op.id})});
     location.href='/notebook/'+encodeURIComponent(created.id);
   }catch(e){setMergeError(e.message)}finally{setMergeBusy(false)}
 }
 function conflictCellSummary(doc, key){
   if(!key?.startsWith('cell:'))return '';
   const cell=doc?.cells?.find(c=>c.id===key.slice(5));
   return cell?sourceText(cell.source).slice(0,260):'[Cell deleted]';
 }
 async function forkVersion(){
   if(!selectedVersion||forking)return;
   setForking(true);
   try{
     const created=await api(`/notebooks/${id}/fork`,{method:'POST',body:JSON.stringify({version:Number(selectedVersion)})});
     setHistoryOpen(false);
     location.href='/notebook/'+encodeURIComponent(created.id);
   }catch(e){setError(e.message)}finally{setForking(false)}
 }
 async function exportNb(){try{if(dirty)await save();const res=await api(`/notebooks/${id}/export`);const blob=await res.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=(title||'notebook').replace(/[^a-z\d_-]/gi,'_')+'.ipynb';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),5000)}catch(e){setError(e.message)}}
 async function deleteNb(){if(!confirm('Delete this notebook? Its persistent workspace files will be retained.'))return;try{await api(`/notebooks/${id}`,{method:'DELETE'});back()}catch(e){setError(e.message)}}
 if(!entry||!doc)return <div className="editor-loading"><NotebookPen size={28}/>{error||'Loading notebook...' }<button className="btn" onClick={back}>Back</button></div>;
 return <div className="editor-shell"><header className="editor-header"><div className="header-brand"><button className="logo compact" onClick={back} title="Back to notebooks"><NotebookPen size={19}/></button><div><input className="title-input" value={title} disabled={!canEdit} onChange={e=>changeTitle(e.target.value)} aria-label="Notebook title"/><div className="subtitle-small"><span className="bread" onClick={back}>My notebooks</span> / {folderCrumb?<><span className="bread" onClick={()=>{location.href='/folder/'+folderCrumb.id}}>{folderCrumb.name}</span> / </>:null}Python 3 <span className="bullet">·</span> <span className="save-state">{saving==='saving'?'Saving...':dirty?'Unsaved changes':'All changes saved'} <span aria-label={liveConnected?'Live collaboration connected':'Live collaboration reconnecting'} title={liveConnected?'Live events connected':'Using polling fallback'}>· {liveConnected?'Live':'Sync'}</span></span></div></div></div><div className="head-buttons"><HeaderButton disabled={!canEdit} onClick={save} title="Save"><Save size={16}/> Save</HeaderButton>{mergeBlockedRef.current&&<HeaderButton onClick={()=>{setMergeOpen(true);inspectMerge()}} title="Resolve unsaved changes against shared latest"><GitMerge size={16}/> Resolve</HeaderButton>}<HeaderButton onClick={exportNb}><Download size={16}/> Export</HeaderButton><HeaderButton onClick={openHistory} title="Browse saved versions or fork a private copy"><BookOpen size={16}/> History / Fork</HeaderButton>{canEdit&&<HeaderButton onClick={()=>setFilesOpen(true)}><FileText size={16}/> Files</HeaderButton>}{entry.role==='owner'&&<HeaderButton onClick={()=>setSharing(true)}><Users size={16}/> Share</HeaderButton>}<button className="avatar top-avatar" title={user.email} onClick={logout}>{user.email[0].toUpperCase()}</button></div></header>
 <div className="editor-toolbar"><div className="toolbar-main"><HeaderButton className="toolbar-primary" disabled={!canEdit} onClick={()=>add(doc.cells.length,'code')}><Plus size={16}/> Code</HeaderButton><HeaderButton disabled={!canEdit} onClick={()=>add(doc.cells.length,'markdown')}><FileText size={16}/> Text</HeaderButton><div className="sep"/><HeaderButton disabled={!canEdit||runtime.status!=='ready'||!channelReady} onClick={runAll}><Play size={16}/> Run all</HeaderButton><HeaderButton disabled={!canEdit||runtime.status!=='ready'} onClick={interrupt}><Square size={15}/> Interrupt</HeaderButton><span className="role-pill"><Shield size={14}/>{entry.role}</span></div><div className="runtime-controls"><span className={'runtime-indicator '+runtime.status}><span/>{runtime.status==='ready'?'Connected':runtime.status==='stopped'?'Disconnected':runtime.status}</span>{runtime.status==='ready'?<><span className="runtime-mode"><Cpu size={15}/> {runtime.mode?.toUpperCase()} {channelReady?"· Kernel ready":"· Kernel disconnected"}</span>{!channelReady&&<HeaderButton onClick={connectChannels}>Reconnect</HeaderButton>}<HeaderButton onClick={stop} disabled={!canEdit||busy}><CircleStop size={16}/> Disconnect</HeaderButton></>:<><select value={choice} onChange={e=>setChoice(e.target.value)} disabled={!canEdit||busy}><option value="cpu">CPU</option>{caps?.gpu_selectable&&<option value="gpu">GPU · {caps.gpu.name?.slice(0,23)}</option>}</select><HeaderButton className="primary" onClick={start} disabled={!canEdit||busy||!caps?.cpu}>{busy?'Connecting...':'Connect'} <Zap size={16}/></HeaderButton></>}</div></div>
 <div className="editor-layout"><main className="cells-panel"><div className="notebook-intro"><small className="eyebrow">INTERACTIVE NOTEBOOK</small><h1>{title}</h1><p>{canEdit?'Write code, explore results, and keep your research in one place.':'Read-only notebook. Contact the owner for editor permission.'}</p></div>{error&&<div className="alert"><AlertCircle size={16}/>{error}<button onClick={()=>setError('')}>×</button></div>}{notice&&<div className="notice"><Check size={15}/>{notice}{mergeBlockedRef.current&&<button className="btn" onClick={()=>{setMergeOpen(true);inspectMerge()}}>Resolve changes</button>}<button onClick={()=>setNotice('')}>×</button></div>}
 {doc.cells.map((cell,i)=><EditorCell key={cell.id} cell={cell} index={i} canEdit={canEdit} update={updateCell} run={run} remove={remove} move={move} add={add} running={!!runningIds[cell.id]}/>)}
 {canEdit&&<div className="end-cell-actions"><HeaderButton onClick={()=>add(doc.cells.length,'code')}><Plus size={16}/> Code cell</HeaderButton><HeaderButton onClick={()=>add(doc.cells.length,'markdown')}><Plus size={16}/> Text cell</HeaderButton></div>}
 <div className="notebook-end">End of notebook <span>·</span> {doc.cells.length} cells</div></main>
 <aside className="details-panel"><div className="details-header"><Activity size={17}/> Environment</div><div className="detail-row"><span>Language</span><strong>Python 3</strong></div><div className="detail-row"><span>Status</span><strong>{runtime.status}</strong></div><div className="detail-row"><span>Compute</span><strong>{runtime.mode?.toUpperCase()||'None'}</strong></div><div className="detail-row"><span>Autosave</span><strong>Enabled</strong></div><div className="detail-row"><span>Cells</span><strong>{doc.cells.length}</strong></div><div className="detail-divider"/><div className="details-header"><Cpu size={17}/> GPU availability</div>{caps?.gpu_selectable?<div className="gpu-good"><Check size={15}/> {caps.gpu.name}</div>:<div className="gpu-off"><AlertCircle size={15}/> GPU not available<div className="gpu-hint">{caps?.gpu?.reason || 'Probing local runtime...'}</div></div>}{!caps?.cpu&&<div className="gpu-off">CPU image missing — build the runtime image first.</div>}<button className="small-link refresh" onClick={async()=>{try{const c=await api('/runtime/capabilities?refresh=true');setCaps(c)}catch(e){setError(e.message)}}}><RefreshCcw size={14}/> Refresh status</button><div className="detail-divider"/><div className="details-header"><Shield size={17}/> Permissions</div><p className="detail-help">{entry.role==='owner'?'You own this notebook and can manage sharing.':entry.role==='editor'?'You can edit and execute cells.':'You can view saved content only.'}</p>{entry.role==='owner'&&<HeaderButton className="danger small" onClick={deleteNb}><Trash2 size={15}/> Delete notebook</HeaderButton>}</aside></div>
 {mergeOpen&&<Modal title="Resolve shared Notebook changes" onClose={()=>setMergeOpen(false)}>
   <div className="modal-body">
     <p className="muted">Your unsaved draft is preserved. Nebula compares its original saved version, your edits, and the latest shared Notebook. No conflict is automatically overwritten.</p>
     <p>Base <strong>v{baselineRef.current?.version}</strong> · Latest <strong>v{mergePreview?.latest_version??'?'}</strong></p>
     <HeaderButton onClick={()=>{mergeOperationRef.current=null;inspectMerge()}} disabled={mergeBusy}>{mergeBusy?'Checking...':'Preview latest changes'}</HeaderButton>
     {mergePreview&&<>
       {mergePreview.conflicts.length===0&&<p className="notice">No overlapping changes detected. The independent edits can be merged.</p>}
       {mergePreview.conflicts.map(conflict=><div key={conflict.key} className="merge-conflict">
         <strong>{conflict.key}</strong><p className="muted">{conflict.message}</p>
         {conflict.type==='cell'&&<div className="merge-versions">
           <div><small>BASE</small><pre>{conflictCellSummary(baselineRef.current?.document,conflict.key)}</pre></div>
           <div><small>MINE</small><pre>{conflictCellSummary(latestDoc.current,conflict.key)}</pre></div>
           <div><small>LATEST</small><pre>{conflictCellSummary(normalize(mergeTheirs?.document||{cells:[]}),conflict.key)}</pre></div>
         </div>}
         <label>Resolve this conflict
           <select value={mergeChoices[conflict.key]||''} onChange={e=>{
             setMergeChoices(prev=>{
               const next={...prev};
               if(e.target.value)next[conflict.key]=e.target.value;
               else delete next[conflict.key];
               return next;
             });
             setMergePreview(previous=>previous?{...previous,can_merge:false}:previous);mergeOperationRef.current=null;
           }}>
             <option value="">Choose explicitly</option><option value="mine">Keep my change</option><option value="theirs">Keep latest shared change</option>
           </select>
         </label>
       </div>)}
       {mergePreview.conflicts.length>0&&<p className="muted">Choose a side for each conflict, then preview again to confirm the merged result.</p>}
     </>}
     {mergeError&&<div className="alert"><AlertCircle size={15}/>{mergeError}</div>}
     <div className="modal-actions">
       <HeaderButton onClick={forkMyUnsavedDraft} disabled={mergeBusy}>Save draft as private fork</HeaderButton>
       <HeaderButton onClick={()=>setMergeOpen(false)}>Keep editing draft</HeaderButton>
       <HeaderButton className="primary" disabled={mergeBusy||!mergePreview?.can_merge}
         onClick={commitResolvedMerge}>Commit merged version</HeaderButton>
     </div>
   </div>
 </Modal>}
 {historyOpen&&<Modal title="Saved versions & private fork" onClose={()=>setHistoryOpen(false)}><div className="modal-body">
   <p className="muted">Saved versions are immutable. Forking creates your own independent Notebook and does not copy runtime variables, workspace files, or collaborators.</p>
   <label>Version to fork<select value={selectedVersion} onChange={e=>setSelectedVersion(e.target.value)}>
     {historyRows.map(r=><option key={r.version} value={r.version}>v{r.version} · {r.reason} · {r.created_at?new Date(r.created_at).toLocaleString():'Saved snapshot'}</option>)}
   </select></label>
   {historyRows.length===0&&<p className="muted">No saved versions available.</p>}
   {hasOlderHistory&&<HeaderButton onClick={loadOlderVersions} disabled={historyLoading}>{historyLoading?'Loading...':'Load older versions'}</HeaderButton>}
   {error&&<div className="alert"><AlertCircle size={16}/>{error}</div>}
   <div className="modal-actions"><HeaderButton onClick={()=>setHistoryOpen(false)}>Cancel</HeaderButton><HeaderButton className="primary" disabled={!selectedVersion||forking} onClick={forkVersion}>{forking?'Forking...':'Fork selected version'}</HeaderButton></div>
 </div></Modal>}
 {sharing&&<ShareDialog id={id} onClose={()=>setSharing(false)}/>}
 {filesOpen&&<FilesDialog id={id} onClose={()=>setFilesOpen(false)}/>}</div>
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function idFromPath(prefix) {
  const match = location.pathname.match(new RegExp(`^/${prefix}/([^/]+)/?$`));
  if (!match) return null;
  try {
    const id = decodeURIComponent(match[1]);
    return UUID.test(id) ? id : null;
  } catch { return null; }
}
// The URL is the single source of truth for "where am I": home / a folder / a notebook.
function routeFromPath() {
  if (location.pathname==='/shared') return {view:'shared',id:null};
  if (location.pathname.startsWith('/notebook/')) return {view:'notebook', id:idFromPath('notebook')};
  if (location.pathname.startsWith('/folder/')) return {view:'folder', id:idFromPath('folder')};
  return {view:'home', id:null};
}
function pathFor(route) {
  if (route.view === 'shared') return '/shared';
  if (route.view === 'notebook' && route.id) return '/notebook/' + encodeURIComponent(route.id);
  if (route.view === 'folder' && route.id) return '/folder/' + encodeURIComponent(route.id);
  return '/';
}

function App(){
  const [user,setUser]=useState(null),[loading,setLoading]=useState(true);
  const [route,setRoute]=useState(()=>routeFromPath());
  useEffect(()=>{api('/auth/me').then(r=>{csrfToken=r.csrf;setUser(r.user)}).catch(()=>{}).finally(()=>setLoading(false))},[]);
  useEffect(()=>{
    const sync=()=>{
      const next=routeFromPath();
      if(location.pathname!=='/'&&!next.id&&next.view!=='shared'){history.replaceState({},'','/');setRoute({view:'home',id:null});return}
      setRoute(next);
    };
    window.addEventListener('popstate',sync);
    sync();
    return()=>window.removeEventListener('popstate',sync);
  },[]);
  const navigate=useCallback(target=>{
    setRoute(target);
    const path=pathFor(target);
    if(location.pathname!==path) history.pushState(target,'',path);
  },[]);
  const open=useCallback(id=>navigate({view:'notebook',id}),[navigate]);
  const openFolder=useCallback(id=>navigate({view:'folder',id}),[navigate]);
 const openShared=useCallback(()=>navigate({view:'shared',id:null}),[navigate]);
  const home=useCallback(()=>{
    setRoute({view:'home',id:null});
    if(location.pathname!=='/') history.replaceState({view:'home',id:null},'','/');
  },[navigate]);
  async function logout(){try{await api('/auth/logout',{method:'POST'})}catch{}csrfToken='';setUser(null);home()}
  if(loading)return <div className="editor-loading"><NotebookPen size={30}/> Loading Nebula...</div>;
  if(!user)return <Login onLogin={setUser}/>;
  if(route.view==='notebook'&&route.id) return <NotebookEditor key={route.id} id={route.id} user={user} back={home} logout={logout}/>;
  // No key: the dashboard keeps its state (view, sort, search, loaded folders)
  // while moving between the root and a folder.
  return <Dashboard user={user} open={open} openFolder={openFolder} openShared={openShared} sharedOnly={route.view==='shared'} goHome={home} folderId={route.view==='folder'?route.id:null} logout={logout}/>;
}
createRoot(document.getElementById('root')).render(<App/>);
