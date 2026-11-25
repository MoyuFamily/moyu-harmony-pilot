"""Deterministic token-efficient Compact UI Snapshot builder and state differ."""

from __future__ import annotations

import re
from collections import Counter

from moyu_harmony_pilot.ui.models import (
    INTERACTIVE_NODE_TYPES,
    CompactElement,
    CompactSnapshot,
    LayoutDiff,
    UiNode,
    UiTree,
)

_FOLDABLE_CONTAINER_TYPES: frozenset[str] = frozenset(
    {
        "Button",
        "Row",
        "Column",
        "Flex",
        "Stack",
        "RelativeContainer",
        "ListItem",
        "GridItem",
        "TabContent",
        "MenuItem",
        "Toggle",
        "Checkbox",
        "Radio",
    }
)
SENSITIVE_SIGNAL_ENUM: tuple[str, ...] = (
    "password_field",
    "captcha_prompt",
    "payment_confirmation",
    "system_permission_dialog",
    "biometric_prompt",
)

_INPUT_CONTROL_TYPES: frozenset[str] = frozenset(
    {"TextInput", "TextArea", "Input", "PasswordInput", "PinInput"}
)
_PASSWORD_INPUT_RE = re.compile(
    r"\b(?:password|passcode|pin(?:\s*code)?|pwd)\b|密码|口令|PIN\s*码", re.IGNORECASE
)
_SEARCH_INPUT_RE = re.compile(r"\b(?:search|find|filter)\b|搜索|查找|筛选", re.IGNORECASE)
_PASSWORD_ID_RE = re.compile(
    r"(?:^|[.\s-])(?:(?:password|passcode|pin|pwd)[.\s-]*(?:input|edit|field|box|entry|keypad)"
    r"|(?:input|edit|field|box|entry|keypad)[.\s-]*(?:password|passcode|pin|pwd))"
    r"(?=$|[.\s\d-])",
    re.IGNORECASE,
)
_PASSWORD_LABEL_RE = re.compile(
    r"(?:(?:(?:current|new|confirm|device|login|payment|lock\s*screen)\s+){0,2}"
    r"(?:password|passcode|pin(?:\s*code)?|pwd)"
    r"|(?:当前|新|确认|锁屏|支付|交易|登录|开机)?(?:密码|口令|PIN\s*码?))\s*[:：*]?",
    re.IGNORECASE,
)
_PASSWORD_PROMPT_RE = re.compile(
    r"(?:\b(?:enter|input|type|verify|confirm|please\s+set)\s+"
    r"(?:(?:your|the|current|new|device|login|payment|lock\s*screen|\d[- ]digit)\s+){0,3}"
    r"(?:password|passcode|pin(?:\s*code)?)\b"
    r"(?!\s*(?:manager|management|settings|options|autofill)\b)"
    r"|(?:输入|验证|确认|请设置|请解锁).{0,6}(?:密码|口令|PIN(?:\s*码|\b))(?!管理|保险箱))",
    re.IGNORECASE,
)
_CAPTCHA_RE = re.compile(
    r"(captcha|verification\s*code|human\s*verification|security\s*verification"
    r"|图形验证码|滑动验证|滑块验证|安全验证|人机验证|短信验证码|风控验证|身份核验)",
    re.IGNORECASE,
)
_PAYMENT_RE = re.compile(
    r"(confirm\s*pay|pay\s*now|确认支付|立即支付|输入支付密码|指纹支付|人脸支付)",
    re.IGNORECASE,
)
_BIOMETRIC_PROMPT_RE = re.compile(
    r"(?:fingerprint\s*(?:auth|verif|unlock|pay)|face\s*(?:id\b|auth|verif|unlock|recognition)"
    r"|biometric\s*(?:auth|verif|prompt|unlock)|verify\s+your\s+identity"
    r"|(?:指纹|人脸|面容|声纹|生物)(?:验证|认证|解锁|支付|识别确认)"
    r"|请验证(?:指纹|人脸|面容|身份))",
    re.IGNORECASE,
)
_BIOMETRIC_INSTRUCTION_RE = re.compile(
    r"(?:\b(?:place|press|touch|scan|use).{0,25}(?:finger|fingerprint|sensor)\b"
    r"|(?:fingerprint|face|biometric)\s*(?:auth|verif|pay|prompt)"
    r"|(?:指纹|人脸|面容|声纹|生物)(?:验证|认证|支付|识别确认)"
    r"|\b(?:look|position).{0,25}(?:camera|face)\b"
    r"|\b(?:verify|authenticate|confirm).{0,25}(?:fingerprint|face|identity|biometric)\b"
    r"|请.{0,12}(?:验证|识别|认证).{0,6}(?:指纹|人脸|面容|身份)"
    r"|请.{0,8}(?:按压|触摸).{0,8}(?:指纹|传感器)"
    r"|请.{0,8}(?:使用|扫描).{0,8}(?:指纹|人脸|面容)"
    r"|请.{0,8}手指.{0,8}(?:放|置).{0,8}(?:传感器|指纹)"
    r"|请.{0,8}(?:看向|正对|对准).{0,8}(?:摄像头|相机|屏幕))",
    re.IGNORECASE,
)
_PERMISSION_GRANT_RE = re.compile(
    r"(?:仅使用期间允许|始终允许|本次允许|允许访问|允许获取|允许使用|同意授权"
    r"|allow\s+(?:while\s+using|only\s+this\s+time|all\s+the\s+time|access)"
    r"|(?:^|\n)\s*(?:允许|同意|授权|启用|开启|开始录制|立即开始|allow|grant|enable|start\s+now)\s*(?:$|\n))",
    re.IGNORECASE,
)
_PERMISSION_REQUEST_RE = re.compile(
    r"(?:是否允许|请求.{0,16}(?:权限|访问|使用|获取|录制)|允许.{0,24}(?:吗|？|\?)"
    r"|(?:允许|授权|启用).{0,24}(?:访问|使用|获取|读取|录制|控制)"
    r"|\ballow\b.{0,40}\bto\s+(?:access|use|record|control)\b"
    r"|\b(?:allow|grant).{0,60}\?|\b(?:wants|would\s+like)\s+to\s+(?:access|use|record))",
    re.IGNORECASE,
)
_PERMISSION_CONTEXT_RE = re.compile(
    r"(?:权限|位置信息|定位|相机|摄像头|麦克风|录音|通讯录|联系人|存储|相册|短信|通话"
    r"|剪贴板|日历|传感器|悬浮窗|无障碍|辅助功能|屏幕录制|录制屏幕|屏幕共享|屏幕内容|读取通知"
    r"|投屏|屏幕投射|虚拟专用网络|安装应用|通知使用权"
    r"|permission|location|camera|microphone|contacts|storage|sms|call\s*log|clipboard"
    r"|accessibility|screen\s*(?:record|captur|shar)|notification\s*access|calendar|photos"
    r"|overlay|\bvpn\b|install\s+apps|record.{0,12}screen|control.{0,12}device)",
    re.IGNORECASE,
)
_PERMISSION_BUNDLES: frozenset[str] = frozenset(
    {
        "com.ohos.permissionmanager",
        "com.huawei.hmos.permissionmanager",
        "com.ohos.security.privacycenter",
    }
)
_BIOMETRIC_BUNDLES: frozenset[str] = frozenset(
    {
        "com.ohos.useriam.authwidget",
        "com.huawei.hmos.useriam.authwidget",
    }
)


def _truncate(value: str, max_len: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= max_len:
        return cleaned
    return f"{cleaned[: max(1, max_len - 1)]}…"


def _collect_passive_descendant_text(
    node: UiNode,
    *,
    max_len: int,
) -> tuple[str, set[str]]:
    """Aggregate labels from non-interactive visible descendants of a clickable container."""
    labels: list[str] = []
    folded_paths: set[str] = set()

    def _walk(current: UiNode) -> None:
        if not current.visible:
            return
        # Never fold across nested interactive controls; they keep their own @eN ref.
        if current.is_interactive:
            return
        label = current.display_label
        if label:
            if not labels or labels[-1] != label:
                labels.append(label)
            folded_paths.add(current.path)
        for child in current.children:
            _walk(child)

    for child in node.children:
        _walk(child)

    if not labels:
        return ("", folded_paths)
    return (_truncate(" | ".join(labels), max_len), folded_paths)


def _sms_content_paths(tree: UiTree) -> set[str]:
    """Prove displayed message roles within a visible system SMS window.

    Only content fields at these paths are exempt from keyword checks. Explicit
    control types/IDs and every other node still participate in safety detection.
    """
    scopes: dict[str, list[UiNode]] = {}

    def _visit(
        node: UiNode, parent_bundle: str = "", parent_window: str = "", scope: str = ""
    ) -> None:
        if not node.visible:
            return
        if node.bounds.is_valid and not node.bounds.intersects_viewport(*tree.screen_size):
            return
        # Match compact snapshot bundle inheritance, including unresolved new windows.
        bundle = node.bundle_name or (
            parent_bundle if not node.window_id or node.window_id == parent_window else ""
        )
        window = node.window_id or parent_window
        if bundle != parent_bundle or window != parent_window:
            scope = node.path
        if bundle == "com.ohos.mms" and node.bounds.is_valid:
            scopes.setdefault(scope, []).append(node)
        for child in node.children:
            _visit(child, bundle, window, scope)

    _visit(tree.root)
    content_paths: set[str] = set()
    for nodes in scopes.values():
        visible_paths = {node.path for node in nodes}
        for container in nodes:
            short_type = container.type.rsplit(".", 1)[-1]
            if (
                short_type == "RelativeContainer"
                and re.fullmatch(r"ConversationList[0-9]+", container.id)
            ):
                body_id = "message_details_content"
                page_ids = {"conversationListSearchBar", "HdsConversationList"}
            elif (
                short_type == "Column"
                and re.fullmatch(r"MessageListContent[0-9]+", container.id)
            ):
                body_id = "msg_content_text"
                page_ids = {"messageList"}
            else:
                continue
            prefix = container.path + "/"
            if not any(
                node.id in page_ids and not node.path.startswith(prefix) for node in nodes
            ):
                continue
            descendants = [node for node in nodes if node.path.startswith(prefix)]
            # A form inside a purported message must keep its labels as safety evidence.
            if any(
                node.type.rsplit(".", 1)[-1] in _INPUT_CONTROL_TYPES | INTERACTIVE_NODE_TYPES
                for node in container.iter_nodes()
            ):
                continue
            bodies = [node for node in descendants if node.id == body_id]
            if len(bodies) != 1 or bodies[0].type.rsplit(".", 1)[-1] != "Text":
                continue
            body_nodes = list(bodies[0].iter_nodes())
            # Native Text may expose its label through nested Span nodes. Prove the
            # whole content subtree, including visibility, geometry and window ownership.
            if any(node.path not in visible_paths for node in body_nodes) or any(
                node.type.rsplit(".", 1)[-1] != "Span" for node in body_nodes[1:]
            ):
                continue
            if any(node.has_semantic_text for node in body_nodes):
                content_paths.add(container.path)
                content_paths.update(node.path for node in body_nodes)
    return content_paths


def detect_sensitive_signals(tree: UiTree) -> tuple[str, ...]:
    """Detect contextual password, verification, payment, and permission signals.

    Returns only sanitized enum-like signal labels, never user text or secrets.
    """
    signals: set[str] = set()
    has_permission_grant = False
    has_permission_context = False
    has_password_label = False
    keypad_digits: set[str] = set()
    sms_content_paths = _sms_content_paths(tree)

    def _walk(node: UiNode, labels: tuple[str, ...] = (), *, in_menu: bool = False) -> None:
        nonlocal has_permission_grant, has_permission_context, has_password_label
        if not node.visible:
            return
        if node.bundle_name in _PERMISSION_BUNDLES:
            signals.add("system_permission_dialog")
        if node.bundle_name in _BIOMETRIC_BUNDLES:
            signals.add("biometric_prompt")

        short_type = node.type.rsplit(".", 1)[-1]
        is_input = short_type in _INPUT_CONTROL_TYPES
        in_menu = in_menu or short_type in {"MenuItem", "ListItem"} or (
            node.clickable and short_type == "Row"
        )
        content = () if node.path in sms_content_paths else (node.text, node.description, node.hint)
        text_parts = " ".join(part for part in content if part)
        # Bound ID tokens also cover camelCase, without matching e.g. "spinning_input".
        identifier = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", node.id).replace("_", " ")
        input_context = f"{identifier} {text_parts}"
        search_metadata = f"{identifier} {node.description} {node.hint}"
        combined = " ".join(part for part in (node.type, node.id, text_parts) if part)

        if (
            short_type in {"PasswordInput", "PinInput"}
            or ((is_input or not in_menu) and _PASSWORD_ID_RE.search(identifier))
            or _PASSWORD_PROMPT_RE.search(text_parts)
            or (
                is_input
                and not _SEARCH_INPUT_RE.search(search_metadata)
                and (
                    _PASSWORD_INPUT_RE.search(input_context)
                    or any(_PASSWORD_LABEL_RE.fullmatch(label.strip()) for label in labels)
                )
            )
        ):
            signals.add("password_field")

        if not in_menu and _PASSWORD_LABEL_RE.fullmatch(text_parts.strip()):
            has_password_label = True
        if (
            content
            and node.clickable
            and node.text.strip() in "0123456789"
            and len(node.text.strip()) == 1
        ):
            keypad_digits.add(node.text.strip())

        if combined:
            if _CAPTCHA_RE.search(combined):
                signals.add("captcha_prompt")
            if _PAYMENT_RE.search(combined):
                signals.add("payment_confirmation")
            if _BIOMETRIC_INSTRUCTION_RE.search(text_parts) or (
                not in_menu and _BIOMETRIC_PROMPT_RE.search(text_parts)
            ):
                signals.add("biometric_prompt")
            if text_parts:
                if any(
                    _PERMISSION_GRANT_RE.search(part) or _PERMISSION_REQUEST_RE.search(part)
                    for part in content
                ):
                    has_permission_grant = True
                if _PERMISSION_CONTEXT_RE.search(text_parts):
                    has_permission_context = True

        # A separate passive label can name an otherwise unlabeled password/PIN input.
        child_labels = tuple(
            label
            for sibling in (node, *node.children)
            if sibling.visible and not sibling.clickable and sibling.path not in sms_content_paths
            for label in (sibling.text, sibling.description, sibling.hint)
            if label
        )
        for child in node.children:
            _walk(child, child_labels, in_menu=in_menu)

    _walk(tree.root)
    if has_password_label and len(keypad_digits) == 10:
        signals.add("password_field")
    if has_permission_grant and has_permission_context:
        signals.add("system_permission_dialog")
    return tuple(signal for signal in SENSITIVE_SIGNAL_ENUM if signal in signals)


def build_compact_snapshot(
    tree: UiTree,
    *,
    max_elements: int = 120,
    include_text_nodes: bool = True,
    max_text_len: int = 120,
) -> CompactSnapshot:
    """Prune a raw HarmonyOS UI tree into a deterministic `@eN` compact snapshot.

    Guarantees:
    - Invisible subtrees and invalid/off-screen nodes are pruned.
    - Unlabeled clickable containers absorb non-interactive child text to eliminate noise.
    - Interactive nodes are prioritized ahead of passive text nodes if `max_elements` is hit.
    - `@e1`, `@e2`, ... numbering is 100% deterministic for identical layouts.
    """
    if max_elements <= 0:
        raise ValueError("max_elements must be positive.")

    screen_w, screen_h = tree.screen_size
    folded_paths: set[str] = set()
    raw_candidates: list[tuple[int, bool, UiNode, str]] = []
    effective_bundles: dict[str, str] = {}
    order_counter = 0

    def _visit(node: UiNode, parent_bundle: str = "", parent_window: str = "") -> None:
        nonlocal order_counter
        if not node.visible:
            return
        # Some official dumps put the bundle only on the window ancestor.
        # An explicitly different window without its own bundle remains unresolved.
        bundle = node.bundle_name or (
            parent_bundle if not node.window_id or node.window_id == parent_window else ""
        )
        window = node.window_id or parent_window
        effective_bundles[node.path] = bundle

        valid_in_view = node.bounds.is_valid and node.bounds.intersects_viewport(
            screen_w, screen_h
        )

        if valid_in_view and node.is_interactive:
            effective_text = _truncate(node.text, max_text_len) if node.text.strip() else ""
            can_fold = (
                not effective_text
                and not node.description.strip()
                and not node.scrollable
                and (
                    node.clickable
                    or node.long_clickable
                    or node.checkable
                    or node.type in _FOLDABLE_CONTAINER_TYPES
                )
            )
            if can_fold:
                folded_text, paths = _collect_passive_descendant_text(node, max_len=max_text_len)
                if folded_text:
                    effective_text = folded_text
                    folded_paths.update(paths)

            raw_candidates.append((order_counter, True, node, effective_text))
            order_counter += 1
        elif (
            include_text_nodes
            and valid_in_view
            and node.path not in folded_paths
            and node.has_semantic_text
        ):
            effective_text = _truncate(node.text, max_text_len) if node.text.strip() else ""
            raw_candidates.append((order_counter, False, node, effective_text))
            order_counter += 1

        for child in node.children:
            _visit(child, bundle, window)

    _visit(tree.root)

    truncated = len(raw_candidates) > max_elements
    if truncated:
        interactive_items = [item for item in raw_candidates if item[1]]
        passive_items = [item for item in raw_candidates if not item[1]]
        if len(interactive_items) >= max_elements:
            selected = interactive_items[:max_elements]
        else:
            remaining = max_elements - len(interactive_items)
            selected = interactive_items + passive_items[:remaining]
        selected.sort(key=lambda item: item[0])
    else:
        selected = raw_candidates

    elements: list[CompactElement] = []
    for idx, (_order, is_interactive, node, effective_text) in enumerate(selected, start=1):
        elements.append(
            CompactElement(
                ref=f"@e{idx}",
                type=node.type,
                text=effective_text,
                id=node.id,
                description=_truncate(node.description, max_text_len)
                if node.description.strip()
                else "",
                hint=_truncate(node.hint, max_text_len) if node.hint.strip() else "",
                bounds=node.bounds,
                center=node.bounds.center,
                center_norm=node.bounds.center_normalized(screen_w, screen_h),
                interactive=is_interactive,
                clickable=node.clickable,
                long_clickable=node.long_clickable,
                scrollable=node.scrollable,
                enabled=node.enabled,
                checked=node.checked,
                selected=node.selected,
                focused=node.focused,
                path=node.path,
                bundle_name=effective_bundles[node.path],
            )
        )

    return CompactSnapshot(
        layout_hash=tree.layout_hash,
        screen_size=tree.screen_size,
        bundle_names=tree.bundle_names,
        total_nodes=tree.node_count,
        kept_elements=len(elements),
        truncated=truncated,
        elements=tuple(elements),
        sensitive_signals=detect_sensitive_signals(tree),
    )


def _element_signature(elem: CompactElement) -> str:
    label = elem.text or elem.description or elem.hint or elem.id or elem.path
    return f"[{elem.type}] {label}"


def diff_snapshots(
    before: CompactSnapshot,
    after: CompactSnapshot,
    *,
    max_samples: int = 10,
) -> LayoutDiff:
    """Compute semantic diff between two `CompactSnapshot` captures."""
    before_counts = Counter(_element_signature(e) for e in before.elements)
    after_counts = Counter(_element_signature(e) for e in after.elements)

    added_counter = after_counts - before_counts
    removed_counter = before_counts - after_counts

    added_list = sorted(added_counter.keys())
    removed_list = sorted(removed_counter.keys())

    return LayoutDiff(
        changed=before.layout_hash != after.layout_hash,
        before_hash=before.layout_hash,
        after_hash=after.layout_hash,
        before_bundles=before.bundle_names,
        after_bundles=after.bundle_names,
        bundle_changed=before.bundle_names != after.bundle_names,
        added_count=sum(added_counter.values()),
        removed_count=sum(removed_counter.values()),
        added_samples=tuple(added_list[:max_samples]),
        removed_samples=tuple(removed_list[:max_samples]),
    )
