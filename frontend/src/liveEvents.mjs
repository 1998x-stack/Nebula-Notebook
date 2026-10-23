/* Pure event reducer. Return null for stale/unsafe patches (caller refetches). */
const asText=x=>Array.isArray(x)?x.join(''):(x||'');
function normalizeCell(c){const {outputs,execution_count,...rest}=c;return c.cell_type==='code'
?{...rest,source:asText(c.source),outputs:outputs||[],execution_count:execution_count??null}
:{...rest,source:asText(c.source)}}
export function applyNotebookDelta(doc,payload,version,nextVersion){
 if(!doc||!payload||payload.full_sync||nextVersion!==version+1||
 !Array.isArray(payload.order)||!Array.isArray(payload.changed)||
 !Array.isArray(payload.removed))return null;
 const cells=new Map(doc.cells.map(c=>[c.id,c]));
 if(cells.size!==doc.cells.length)return null;
 for(const id of payload.removed)cells.delete(id);
 for(const c of payload.changed){if(!c||typeof c.id!=='string')return null;cells.set(c.id,normalizeCell(c));}
 if(payload.order.length!==cells.size||new Set(payload.order).size!==cells.size||
 payload.order.some(id=>!cells.has(id)))return null;
 return {...doc,cells:payload.order.map(id=>cells.get(id))};
}
export function mergeRemoteOutput(doc,id,outputs){
 if(!doc||!Array.isArray(outputs)||outputs.length>100||!doc.cells.some(c=>c.id===id))return null;
 return {...doc,cells:doc.cells.map(c=>c.id===id?{...c,outputs:[...(c.outputs||[]),...outputs].slice(-200)}:c)};
}
