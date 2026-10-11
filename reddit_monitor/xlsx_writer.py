import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
ET.register_namespace("", NS_MAIN)
ET.register_namespace("r", NS_REL)


def _col_letter(n):
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(row, col_idx, value, style=0):
    ref = "%s%d" % (_col_letter(col_idx), row)
    c = ET.Element("{%s}c" % NS_MAIN, {"r": ref, "t": "inlineStr"})
    if style:
        c.set("s", str(style))
    is_el = ET.SubElement(c, "{%s}is" % NS_MAIN)
    t = ET.SubElement(is_el, "{%s}t" % NS_MAIN)
    text = "" if value is None else str(value)
    if text[:1].isspace() or text[-1:].isspace():
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    return c


def _sheet_xml(headers, rows, hyperlink_col=None):
    ws = ET.Element("{%s}worksheet" % NS_MAIN)
    views = ET.SubElement(ws, "{%s}sheetViews" % NS_MAIN)
    view = ET.SubElement(views, "{%s}sheetView" % NS_MAIN, {"workbookViewId": "0"})
    ET.SubElement(view, "{%s}pane" % NS_MAIN, {"ySplit": "1", "topLeftCell": "A2", "activePane": "bottomLeft", "state": "frozen"})
    cols = ET.SubElement(ws, "{%s}cols" % NS_MAIN)
    for idx, header in enumerate(headers, 1):
        max_len = len(str(header))
        for row in rows[:200]:
            if idx - 1 < len(row):
                max_len = max(max_len, len(str(row[idx - 1] or "")))
        cap = 72 if "Description" in str(header) else 52 if "Title" in str(header) else 34
        width = min(cap, max(10, max_len + 2))
        ET.SubElement(cols, "{%s}col" % NS_MAIN, {"min": str(idx), "max": str(idx), "width": "%.1f" % width, "customWidth": "1"})
    data = ET.SubElement(ws, "{%s}sheetData" % NS_MAIN)
    row_el = ET.SubElement(data, "{%s}row" % NS_MAIN, {"r": "1"})
    for idx, header in enumerate(headers, 1):
        row_el.append(_cell(1, idx, header, style=1))
    rels = []
    hyperlink_items = []
    for ridx, row in enumerate(rows, 2):
        r = ET.SubElement(data, "{%s}row" % NS_MAIN, {"r": str(ridx)})
        for cidx, value in enumerate(row, 1):
            r.append(_cell(ridx, cidx, value, style=2 if cidx == hyperlink_col else 0))
        if hyperlink_col and len(row) >= hyperlink_col and row[hyperlink_col - 1]:
            rel_id = "rId%d" % (len(rels) + 1)
            rels.append((rel_id, str(row[hyperlink_col - 1])))
            hyperlink_items.append(("%s%d" % (_col_letter(hyperlink_col), ridx), rel_id))
    if hyperlink_items:
        hlinks = ET.SubElement(ws, "{%s}hyperlinks" % NS_MAIN)
        for ref, rel_id in hyperlink_items:
            ET.SubElement(hlinks, "{%s}hyperlink" % NS_MAIN, {"ref": ref, "{%s}id" % NS_REL: rel_id})
    ET.SubElement(ws, "{%s}autoFilter" % NS_MAIN, {"ref": "A1:%s%d" % (_col_letter(len(headers)), max(1, len(rows) + 1))})
    return ET.tostring(ws, encoding="utf-8", xml_declaration=True), rels


def _rels_xml(items):
    root = ET.Element("Relationships", xmlns=NS_PKG_REL)
    for rel_id, target in items:
        ET.SubElement(root, "Relationship", {
            "Id": rel_id,
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            "Target": target,
            "TargetMode": "External",
        })
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def write_xlsx(path, sheets):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    content_types = ET.Element("Types", xmlns=NS_CT)
    ET.SubElement(content_types, "Default", {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"})
    ET.SubElement(content_types, "Default", {"Extension": "xml", "ContentType": "application/xml"})
    ET.SubElement(content_types, "Override", {"PartName": "/xl/workbook.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"})
    ET.SubElement(content_types, "Override", {"PartName": "/xl/styles.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"})
    ET.SubElement(content_types, "Override", {"PartName": "/docProps/core.xml", "ContentType": "application/vnd.openxmlformats-package.core-properties+xml"})
    ET.SubElement(content_types, "Override", {"PartName": "/docProps/app.xml", "ContentType": "application/vnd.openxmlformats-officedocument.extended-properties+xml"})
    for i in range(1, len(sheets) + 1):
        ET.SubElement(content_types, "Override", {"PartName": "/xl/worksheets/sheet%d.xml" % i, "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"})

    workbook = ET.Element("{%s}workbook" % NS_MAIN)
    sheets_el = ET.SubElement(workbook, "{%s}sheets" % NS_MAIN)
    for i, sheet in enumerate(sheets, 1):
        ET.SubElement(sheets_el, "{%s}sheet" % NS_MAIN, {"name": sheet["name"], "sheetId": str(i), "{%s}id" % NS_REL: "rId%d" % i})

    wb_rels = ET.Element("Relationships", xmlns=NS_PKG_REL)
    for i in range(1, len(sheets) + 1):
        ET.SubElement(wb_rels, "Relationship", {"Id": "rId%d" % i, "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet", "Target": "worksheets/sheet%d.xml" % i})
    ET.SubElement(wb_rels, "Relationship", {"Id": "rId%d" % (len(sheets) + 1), "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles", "Target": "styles.xml"})

    package_rels = ET.Element("Relationships", xmlns=NS_PKG_REL)
    ET.SubElement(package_rels, "Relationship", {"Id": "rId1", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument", "Target": "xl/workbook.xml"})
    ET.SubElement(package_rels, "Relationship", {"Id": "rId2", "Type": "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "Target": "docProps/core.xml"})
    ET.SubElement(package_rels, "Relationship", {"Id": "rId3", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties", "Target": "docProps/app.xml"})

    styles = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font><font><u/><color rgb="FF0563C1"/><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFD9EAF7"/><bgColor indexed="64"/></patternFill></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
    core = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>Reddit RSS Monitor</dc:creator><dc:title>Reddit Signals</dc:title></cp:coreProperties>'''
    app = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Reddit RSS Monitor</Application></Properties>'''

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ET.tostring(content_types, encoding="utf-8", xml_declaration=True))
        z.writestr("_rels/.rels", ET.tostring(package_rels, encoding="utf-8", xml_declaration=True))
        z.writestr("xl/workbook.xml", ET.tostring(workbook, encoding="utf-8", xml_declaration=True))
        z.writestr("xl/_rels/workbook.xml.rels", ET.tostring(wb_rels, encoding="utf-8", xml_declaration=True))
        z.writestr("xl/styles.xml", styles)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        for i, sheet in enumerate(sheets, 1):
            xml, rels = _sheet_xml(sheet["headers"], sheet["rows"], sheet.get("hyperlink_col"))
            z.writestr("xl/worksheets/sheet%d.xml" % i, xml)
            if rels:
                z.writestr("xl/worksheets/_rels/sheet%d.xml.rels" % i, _rels_xml(rels))
    tmp.replace(path)
    return path


def validate_xlsx(path, expected_sheet_names=None):
    path = Path(path)
    with zipfile.ZipFile(path, "r") as z:
        required = {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"}
        names = set(z.namelist())
        missing = required - names
        if missing:
            raise ValueError("Missing XLSX parts: %s" % sorted(missing))
        # Every XML part must parse.
        for name in names:
            if name.endswith((".xml", ".rels")):
                ET.fromstring(z.read(name))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheet_names = [s.attrib["name"] for s in wb.findall("{%s}sheets/{%s}sheet" % (NS_MAIN, NS_MAIN))]
        if expected_sheet_names is not None and sheet_names != list(expected_sheet_names):
            raise ValueError("Unexpected sheet names: %r" % sheet_names)
        row_counts = []
        hyperlink_counts = []
        for i in range(1, len(sheet_names) + 1):
            root = ET.fromstring(z.read("xl/worksheets/sheet%d.xml" % i))
            rows = root.findall("{%s}sheetData/{%s}row" % (NS_MAIN, NS_MAIN))
            row_counts.append(max(0, len(rows) - 1))
            hyperlinks = root.findall("{%s}hyperlinks/{%s}hyperlink" % (NS_MAIN, NS_MAIN))
            hyperlink_counts.append(len(hyperlinks))
            if hyperlinks:
                rel_path = "xl/worksheets/_rels/sheet%d.xml.rels" % i
                if rel_path not in names:
                    raise ValueError("Hyperlinks exist without relationships in sheet %d" % i)
        return {"sheet_names": sheet_names, "row_counts": row_counts, "hyperlink_counts": hyperlink_counts}
