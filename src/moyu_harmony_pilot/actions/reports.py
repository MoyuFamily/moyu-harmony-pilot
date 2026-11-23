"""Self-contained private HTML inspection and execution reports."""

from __future__ import annotations

import base64
import html
import json
import tempfile
from pathlib import Path
from typing import Any

from moyu_harmony_pilot.device import DeviceConfigurationError, HarmonyDevice, write_secure_text
from moyu_harmony_pilot.ui import CompactSnapshot, UiError

STYLE = """
:root { color-scheme: light dark; font-family: Inter, system-ui, sans-serif; }
body { margin: 0 auto; padding: 28px; max-width: 1200px; background: #101820; color: #edf3f5; }
h1 { font-size: 26px; margin: 0 0 12px; }
p { color: #aec1cc; overflow-wrap: anywhere; }
main { display: grid; grid-template-columns: minmax(220px, 400px) 1fr; gap: 24px; }
.screen { position: relative; align-self: start; }
.screen img { display: block; width: 100%; }
.box { position: absolute; border: 1px solid #51dcbb; background: #51dcbb0c; cursor: pointer; }
.box.active { background: #51dcbb66; outline: 2px solid white; z-index: 2; }
button { font: inherit; color: inherit; }
.element { display: block; text-align: left; width: 100%; border: 1px solid #334955;
  background: #172630; margin: 6px 0; padding: 10px; border-radius: 6px; overflow-wrap: anywhere; }
.element.active { border-color: #51dcbb; }
.list { max-height: 60vh; overflow: auto; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #172630; padding: 16px; }
table { border-collapse: collapse; width: 100%; margin: 20px 0; }
td, th { border-bottom: 1px solid #334955; text-align: left; padding: 10px;
  overflow-wrap: anywhere; }
input { box-sizing: border-box; width: 100%; padding: 10px; font: inherit; }
@media (max-width: 700px) { main { grid-template-columns: 1fr; } .screen { max-width: 320px; } }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
        "img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'none'\">"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style><body>{body}</body></html>"
    )


def render_inspector(snapshot: CompactSnapshot, png: bytes, *, alias: str) -> str:
    width, height = snapshot.screen_size
    if width <= 0 or height <= 0:
        raise UiError("Inspector requires known display dimensions.")
    boxes, rows = [], []
    for index, element in enumerate(snapshot.elements):
        left, top, right, bottom = element.bounds.to_list()
        left, right = max(0, left), min(width, right)
        top, bottom = max(0, top), min(height, bottom)
        if left < right and top < bottom:
            boxes.append(
                f"<button class='box' data-index='{index}' aria-label='{element.ref}' style='"
                f"left:{left / width * 100:.4f}%;top:{top / height * 100:.4f}%;"
                f"width:{(right - left) / width * 100:.4f}%;"
                f"height:{(bottom - top) / height * 100:.4f}%'></button>"
            )
        label = html.escape(
            f"{element.ref} {element.type} {element.text or element.description or element.id}"
        )
        rows.append(f"<button class='element' data-index='{index}'>{label}</button>")
    data = json.dumps(snapshot.to_dict(), ensure_ascii=True)
    # JSON is base64-encoded to keep arbitrary observed text outside executable script markup.
    encoded = base64.b64encode(data.encode()).decode()
    picture = base64.b64encode(png).decode()
    body = (
        f"<h1>界面检查 · {html.escape(alias)}</h1>"
        f"<p>布局 {html.escape(snapshot.layout_hash)} · {snapshot.kept_elements} 个元素</p>"
        "<main><div class='screen'>"
        f"<img alt='设备界面截图' src='data:image/png;base64,{picture}'>"
        + "".join(boxes)
        + "</div><section><input id='filter' aria-label='筛选元素' "
        "placeholder='搜索文字、类型或元素编号'><div class='list'>"
        + "".join(rows)
        + "</div><h2>元素属性</h2><pre id='detail'>点击截图边框或元素查看属性</pre>"
        "</section></main>"
        + f"<script>const snapshot = JSON.parse(atob('{encoded}'));"
        + """
document.querySelectorAll('[data-index]').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('[data-index]').forEach(item =>
      item.classList.toggle('active', item.dataset.index === button.dataset.index));
    document.getElementById('detail').textContent =
      JSON.stringify(snapshot.elements[Number(button.dataset.index)], null, 2);
  });
});
document.getElementById('filter').addEventListener('input', event => {
  const query = event.target.value.toLowerCase();
  document.querySelectorAll('.element').forEach(item => {
    item.hidden = !item.textContent.toLowerCase().includes(query);
    item.style.display = item.hidden ? 'none' : 'block';
  });
});
</script>"""
    )
    return _page("Harmony Pilot 界面检查", body)


def capture_inspector(
    device: HarmonyDevice, output: str | Path, *, timeout: float = 30,
) -> dict[str, str | int]:
    with device.acquire_lock(), tempfile.TemporaryDirectory(prefix="moyu-inspect-") as directory:
        _, before = device.dump_ui_compact(timeout=timeout)
        image = device.screenshot(Path(directory) / "screen.png", timeout=timeout)
        _, after = device.dump_ui_compact(timeout=timeout)
        if before.layout_hash != after.layout_hash:
            raise UiError(
                "UI changed during inspector capture.", code="layout_changed", retryable=True
            )
        page = render_inspector(before, image.path.read_bytes(), alias=device.alias)
    path = write_secure_text(output, page)
    return {
        "alias": device.alias,
        "path": str(path),
        "layout_hash": before.layout_hash,
        "elements": before.kept_elements,
    }


def write_report(payload: dict[str, Any] | list[Any], output: str | Path) -> Path:
    if isinstance(payload, dict):
        content = payload.get("data", payload)
        if content is None:
            content = payload.get("error", {})
    else:
        content = payload
    if isinstance(content, dict) and isinstance(content.get("records"), list):
        content = content["records"]
    records = content if isinstance(content, list) else [content]
    if not all(isinstance(record, dict) for record in records):
        raise DeviceConfigurationError("Report requires object records.")
    rows = []
    for record in records:
        receipt = record.get("receipt") or record
        if not isinstance(receipt, dict):
            raise DeviceConfigurationError("Report receipt must be an object.")
        cells = [
            record.get("action_id", record.get("action", "执行记录")),
            record.get("status", receipt.get("status", "—")),
            record.get("updated_at", "—"),
            receipt.get("summary", ""),
        ]
        rows.append(
            "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells) + "</tr>"
        )
    details = html.escape(json.dumps(payload, ensure_ascii=False, indent=2))
    body = (
        "<h1>Harmony Pilot 执行报告</h1><table><thead><tr><th>动作</th><th>状态</th>"
        "<th>更新时间</th><th>结果</th></tr></thead><tbody>"
        + "".join(rows)
        + f"</tbody></table><details><summary>完整记录</summary><pre>{details}</pre></details>"
    )
    return write_secure_text(output, _page("Harmony Pilot 执行报告", body))
