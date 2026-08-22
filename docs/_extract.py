import zipfile, xml.etree.ElementTree as ET, sys, traceback

try:
    src = r'd:/HCM_ASST/docs/和乘幂信号策略开发文档.docx'
    z = zipfile.ZipFile(src)
    xml = z.read('word/document.xml').decode('utf-8')
    W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    root = ET.fromstring(xml)
    paras = []
    for p in root.iter(W + 'p'):
        texts = [t.text for t in p.iter(W + 't') if t.text]
        paras.append(''.join(texts))
    out = '\n'.join(paras)
    with open(r'd:/HCM_ASST/docs/_hepow.txt', 'w', encoding='utf-8') as f:
        f.write(out)
    with open(r'd:/HCM_ASST/docs/_hepow_len.txt', 'w', encoding='utf-8') as f:
        f.write(str(len(out)))
except Exception:
    with open(r'd:/HCM_ASST/docs/_hepow_err.txt', 'w', encoding='utf-8') as f:
        f.write(traceback.format_exc())
