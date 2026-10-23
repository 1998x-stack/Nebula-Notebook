import json
import nbformat
from fastapi import HTTPException
from .config import settings


def default_document():
    return {
        'cells': [
            {'cell_type': 'markdown', 'id': 'welcome', 'metadata': {}, 'source': '# Welcome to Nebula Notebook\nEdit and run the code below.'},
            {'cell_type': 'code', 'id': 'example', 'metadata': {}, 'source': 'print("Hello, Nebula Notebook!")', 'execution_count': None, 'outputs': []},
        ],
        'metadata': {'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'}},
        'nbformat': 4, 'nbformat_minor': 5,
    }


def checked_document(doc):
    try:
        raw = json.dumps(doc, ensure_ascii=False)
        if len(raw.encode()) > settings.max_notebook_bytes:
            raise ValueError('Notebook exceeds configured size limit')
        for cell in doc.get('cells', []):
            source = cell.get('source', '')
            if isinstance(source, list):
                source = ''.join(source)
            if len(source) > settings.max_cell_source_chars:
                raise ValueError('Cell source exceeds size limit')
        notebook = nbformat.from_dict(doc)
        nbformat.validate(notebook)
        return json.loads(nbformat.writes(notebook))
    except (ValueError, KeyError, TypeError, nbformat.ValidationError) as exc:
        raise HTTPException(422, f'Invalid notebook document: {str(exc)[:250]}') from exc
