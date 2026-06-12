"""
Playwright 集成层 - 在浏览器中通过大模型解算验证码

支持点击型和滑块型腾讯验证码。
参考 ha-95598 项目的 DOM 操作方式。
"""

import io
import logging
import random
import re
import time
from typing import List, Optional, Tuple

import requests
from PIL import Image
from playwright.sync_api import Page, ElementHandle
from click_captcha_solver import ClickCaptchaSolver
from openai import OpenAI

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 腾讯验证码 DOM 选择器
# ═══════════════════════════════════════════════════════════

TENCENT_SELECTORS = {
    "content": "#tCaptchaDyContent",
    "header_answer_img": ".tencent-captcha-dy__header-answer img",
    "point_area": ".tencent-captcha-dy__point-area",
    "click_type_wrap": ".tencent-captcha-dy__click-type-wrap",
    "image_area": ".tencent-captcha-dy__verify-bg-img",
    "verify_bg_img": ".tencent-captcha-dy__verify-bg-img",
    "verify_bg": ".tencent-captcha-dy__verify-bg",
    "refresh_btn": ".tencent-captcha-dy__footer-icon--refresh",
    "confirm_btn": ".tencent-captcha-dy__verify-confirm-btn",
    "slider_area": ".tencent-captcha-dy__verify-slider-area",
    "slider_groove": ".tencent-captcha-dy__slider-groove",
    "slider_block": ".tencent-captcha-dy__slider-block",
    "slider_bg_img": ".tencent-captcha-dy__slider-bg-img",
}

# 备选 widget 选择器（参考 ha-95598）
_WIDGET_SELECTORS = [
    ".tencent-captcha-dy__warp",
    ".tencent-captcha-dy__wrapper",
    ".tencent-captcha__wrapper",
    ".tencent-captcha-dy__body-wrap",
    "#tCaptchaDyContent",
]


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

def solve_captcha_in_browser(page: Page,
                             timeout: int = 15,
                             max_retries: int = 3,
                             selectors: dict = None,
                             solver: ClickCaptchaSolver = None) -> bool:
    """在浏览器中处理验证码，返回是否通过。"""
    selectors = selectors or TENCENT_SELECTORS
    solver = solver or ClickCaptchaSolver()

    for attempt in range(max_retries):
        logger.info(f"验证码尝试 {attempt + 1}/{max_retries}")

        if not _wait_for_captcha(page, selectors, timeout):
            logger.warning("验证码未出现")
            continue

        captcha_type = _detect_captcha_type_js(page)
        logger.info(f"验证码类型: {captcha_type}")

        if captcha_type == "slider":
            if _solve_slider(page, selectors):
                logger.info("滑块已解算！")
                return True
            _refresh_captcha(page, selectors)
            time.sleep(2)
            continue

        if captcha_type != "click":
            # 尝试刷新获取点击型
            for refresh_i in range(5):
                logger.info(f"获取到 {captcha_type}，正在刷新 ({refresh_i + 1}/5)...")
                _refresh_captcha(page, selectors)
                time.sleep(2)
                captcha_type = _detect_captcha_type_js(page)
                logger.info(f"刷新后验证码类型: {captcha_type}")
                if captcha_type == "click":
                    break
                if captcha_type == "slider":
                    if _solve_slider(page, selectors):
                        logger.info("刷新后滑块已解算！")
                        return True
            if captcha_type != "click":
                continue

        # 提取图片 URL
        ref_url = _extract_ref_url(page, selectors)
        main_url, main_size = _extract_main_url(page, selectors)

        if not ref_url or not main_url:
            logger.warning("提取验证码图片URL失败，正在刷新...")
            _refresh_captcha(page, selectors)
            time.sleep(1)
            continue

        logger.info(f"主图尺寸={main_size}")

        _save_debug_images(ref_url, main_url)

        # 调用大模型解算
        coords = solver.solve(ref_url, main_url, main_size[0], main_size[1])
        if not coords or len(coords) < 2:
            logger.warning(f"从大模型仅获取到 {len(coords)} 个坐标，正在刷新...")
            _refresh_captcha(page, selectors)
            time.sleep(1)
            continue
        logger.info(f"大模型坐标: {coords}")

        # 获取主图元素用于坐标转换（传入期望宽高比）
        expected_aspect = main_size[0] / main_size[1]
        image_el = _find_main_image_element(page, selectors, expected_aspect)
        if image_el is None:
            logger.error("找不到主图元素")
            continue

        rect = page.evaluate(
            "el => { var r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; }",
            image_el
        )
        if rect["h"] < 10:
            image_el = _find_element(page, selectors.get("image_area"))
            if image_el is None:
                continue
            rect = page.evaluate(
                "el => { var r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; }",
                image_el
            )

        scale_x = rect["w"] / main_size[0]
        scale_y = rect["h"] / main_size[1]
        logger.info(f"图片区域: rect={rect}, 缩放=({scale_x:.3f}, {scale_y:.3f})")

        # 按顺序点击
        for i, (cx, cy) in enumerate(coords[:3]):
            # 坐标转换：图片像素 → 元素CSS像素
            px = cx * scale_x
            py = cy * scale_y
            # Playwright 点击，相对于元素的中心偏移
            offset_x = px - rect["w"] / 2
            offset_y = py - rect["h"] / 2
            logger.info(f"点击 #{i + 1}: 像素=({cx},{cy}) -> 偏移=({offset_x},{offset_y})")
            _click_with_actions(page, image_el, offset_x, offset_y)
            time.sleep(random.uniform(0.25, 0.55))

        time.sleep(1)

        # 等待确认按钮可用并点击
        confirm_btn = _find_element(page, selectors.get("confirm_btn"))
        if confirm_btn is not None and confirm_btn.is_visible():
            try:
                page.wait_for_function(
                    "el => !el.classList.contains('disabled') && !el.hasAttribute('disabled')",
                    arg=confirm_btn,
                    timeout=3000
                )
                logger.info("确认按钮已启用，正在点击...")
                confirm_btn.click()
                time.sleep(2)
            except Exception:
                logger.info("点击后确认按钮仍为禁用状态")

        time.sleep(2)
        if _check_passed(page, selectors):
            logger.info("验证码已通过！")
            return True

        logger.info("未通过，正在刷新...")
        _refresh_captcha(page, selectors)
        time.sleep(1)

    logger.error("所有重试后验证码解算失败")
    return False


# ═══════════════════════════════════════════════════════════
# 滑块验证码
# ═══════════════════════════════════════════════════════════

def _solve_slider(page: Page, selectors: dict) -> bool:
    """使用LLM解算滑块验证码：识别缺口位置 → 模拟拖拽。"""
    # 提取滑块背景图
    bg_el = _find_element(page, ".tencent-captcha-dy__slider-bg-img", wait=1.0)
    if bg_el is None:
        bg_el = _find_element(page, selectors.get("verify_bg_img"), wait=1.0)
    if bg_el is None:
        logger.warning("找不到滑块背景图片")
        return False

    # 尝试获取滑块背景图 URL
    bg_url = None
    tag = (page.evaluate("el => el.tagName", bg_el) or "").lower()
    if tag == "img":
        bg_url = bg_el.get_attribute("src") or ""
    if not bg_url or not bg_url.startswith("http"):
        style = bg_el.get_attribute("style") or ""
        m = re.search(r'url\(["\']?(https?://[^"\')\s]+)["\']?\)', style)
        if m:
            bg_url = m.group(1)

    if not bg_url:
        # 截图回退
        try:
            bg_bytes = bg_el.screenshot()
            bg_url = "data:image/png;base64," + __import__('base64').b64encode(bg_bytes).decode()
        except Exception as e:
            logger.error(f"无法获取滑块背景图片，错误详情: {e}")  
            return False

    # 获取滑块容器宽度用于距离计算
    groove = _find_element(page, selectors.get("slider_groove"), wait=1.0)
    slider_block = _find_element(page, selectors.get("slider_block"), wait=1.0)

    if groove is None or slider_block is None:
        logger.warning("找不到滑块轨道/滑块块")
        return False

    groove_rect = groove.bounding_box()
    groove_width = groove_rect.get("width", 300) if groove_rect else 300
    logger.info(f"滑块轨道宽度: {groove_width}")

    # 下载背景图并调用LLM识别缺口位置
    try:
        import base64
        import const
        client = OpenAI(base_url=const.LLM_BASE_URL, api_key=const.LLM_API_KEY)

        if bg_url.startswith("http"):
            resp = requests.get(bg_url, timeout=15)
            bg_data = resp.content
        elif bg_url.startswith("data:"):
            _, encoded = bg_url.split(",", 1)
            bg_data = base64.b64decode(encoded)
        else:
            return False

        bg_uri = "data:image/png;base64," + base64.b64encode(bg_data).decode()
        img = Image.open(io.BytesIO(bg_data))
        bg_w, bg_h = img.size

        response = client.chat.completions.create(
            model=const.LLM_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": bg_uri}},
                    {"type": "text", "text": (
                        f"这是一个滑块拼图验证码的背景图（{bg_w}x{bg_h}像素）。\n"
                        "图中有一个矩形缺口（拼图块被挖掉的位置），缺口边缘有轻微阴影或颜色差异。\n"
                        "请找到这个缺口，返回缺口左侧边缘的X坐标比例（0~1之间）。\n"
                        "输出格式（仅一个数字）：0.XX"
                    )},
                ],
            }],
            max_tokens=50,
        )

        output = response.choices[0].message.content or ""
        logger.info(f"滑块大模型响应: {output[:100]}")

        # 解析比例
        nums = re.findall(r'(\d+\.?\d*)', output)
        if not nums:
            logger.warning("无法从大模型解析滑块位置")
            return False
        ratio = float(nums[0])
        if ratio > 1.5:
            ratio = ratio / bg_w
        ratio = max(0.0, min(1.0, ratio))
        logger.info(f"滑块缺口比例: {ratio:.3f}")

        # 计算拖拽距离
        slider_rect = slider_block.bounding_box()
        slider_width = slider_rect.get("width", 40) if slider_rect else 40
        track_width = groove_width - slider_width
        drag_distance = int(ratio * track_width)
        drag_distance = max(10, min(drag_distance, track_width))
        logger.info(f"拖拽距离: {drag_distance}px (轨道={track_width})")

        # 模拟拖拽（分段移动，模拟人类行为）
        _simulate_drag(page, slider_block, drag_distance)
        time.sleep(2)

        return _check_passed(page, selectors)

    except Exception as e:
        logger.error(f"滑块解算错误: {e}")
        return False


def _simulate_drag(page: Page, element: ElementHandle, distance: int):
    """模拟人类拖拽滑块，分段移动 + 随机停顿。"""
    try:
        box = element.bounding_box()
        if not box:
            return
        
        start_x = box['x'] + box['width'] / 2
        start_y = box['y'] + box['height'] / 2
        
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        time.sleep(random.uniform(0.05, 0.15))

        # 分段移动（3-5段）
        segments = random.randint(3, 5)
        current_x = start_x
        remaining = distance
        for _ in range(segments - 1):
            step = random.randint(int(remaining * 0.2), int(remaining * 0.5))
            remaining -= step
            current_x += step
            page.mouse.move(current_x, start_y + random.randint(-1, 1), steps=5)
            time.sleep(random.uniform(0.02, 0.08))

        # 最后一段
        page.mouse.move(start_x + distance, start_y + random.randint(-1, 1), steps=5)
        time.sleep(random.uniform(0.1, 0.2))
        page.mouse.up()

        logger.info(f"拖拽 {distance}px，共 {segments} 段")
    except Exception as e:
        logger.error(f"拖拽错误: {e}")


# ═══════════════════════════════════════════════════════════
# 图片 URL 提取
# ═══════════════════════════════════════════════════════════

def _extract_ref_url(page: Page, selectors: dict) -> Optional[str]:
    """提取参考图标条的图片 URL。"""
    el = _find_element(page, selectors.get("header_answer_img"))
    if el is None:
        return None
    src = el.get_attribute("src") or ""
    if src:
        return src
    return None


def _extract_main_url(page: Page, selectors: dict) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    """提取主图的 URL 及尺寸。"""
    # 多选择器查找
    for sel in [selectors.get("verify_bg_img"),
                selectors.get("point_area"),
                selectors.get("click_type_wrap"),
                selectors.get("verify_bg")]:
        el = _find_element(page, sel)
        if el is None:
            continue

        tag = (page.evaluate("el => el.tagName", el) or "").lower()
        if tag == "img":
            src = el.get_attribute("src") or ""
            if src:
                size = _get_image_size_from_url(src)
                return src, size

        style = el.get_attribute("style") or ""
        url_match = re.search(r'url\(["\']?(https?://[^"\')\s]+)["\']?\)', style)
        if url_match:
            url = url_match.group(1)
            size = _get_image_size_from_url(url)
            return url, size

    return None, None


def _save_debug_images(ref_url: str, main_url: str):
    try:
        if ref_url.startswith("http"):
            resp = requests.get(ref_url, timeout=15)
            if resp.status_code == 200:
                with open("captcha_ref_strip_debug.png", "wb") as f:
                    f.write(resp.content)
        if main_url.startswith("http"):
            resp = requests.get(main_url, timeout=15)
            if resp.status_code == 200:
                with open("captcha_main_debug.png", "wb") as f:
                    f.write(resp.content)
    except Exception:
        pass


def _get_image_size_from_url(url: str) -> Optional[Tuple[int, int]]:
    if not url or not url.startswith("http"):
        return None
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            img = Image.open(io.BytesIO(resp.content))
            return img.size
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════
# DOM 操作辅助
# ═══════════════════════════════════════════════════════════

def _detect_captcha_type_js(page: Page) -> str:
    """使用 JS 注入检测验证码类型。"""
    try:
        result = page.evaluate("""
            () => {
                function textOf(sel) {
                    var els = document.querySelectorAll(sel);
                    for (var i = 0; i < els.length; i++) {
                        if (els[i].offsetParent !== null) {
                            return (els[i].textContent || els[i].innerText || '').trim();
                        }
                    }
                    return '';
                }
                function exists(sel) {
                    var els = document.querySelectorAll(sel);
                    for (var i = 0; i < els.length; i++) {
                        if (els[i].offsetParent !== null) return true;
                    }
                    return false;
                }

                var prompt = textOf('.tencent-captcha-dy__header-text') ||
                             textOf('.tencent-captcha-dy__question') ||
                             textOf('.tencent-captcha-dy__title') || '';

                if (/拖动|拼图|滑块/i.test(prompt)) return 'slider';

                var hasPointClick = /依次点击|顺序点击|点击下图|文字点选|请点击|点击/i.test(prompt) ||
                                    exists('.tencent-captcha-dy__click-type-wrap') ||
                                    exists('.tencent-captcha-dy__click-word') ||
                                    exists('.tencent-captcha-dy__point-area') ||
                                    exists('.tencent-captcha-dy__header-answer');

                if (hasPointClick) return 'click';

                if (exists('.tencent-captcha-dy__slider-groove') ||
                    exists('.tencent-captcha-dy__verify-slider-area')) return 'slider';

                return 'unknown';
            }
        """)
        return result or "unknown"
    except Exception:
        pass

    return _detect_captcha_type_fallback(page)


def _detect_captcha_type_fallback(page: Page) -> str:
    """回退检测方法（HTML文本扫描）。"""
    content_el = _find_element(page, "#tCaptchaDyContent", wait=1.0)
    if content_el is None:
        return "unknown"
    try:
        html = content_el.inner_html() or ""
        if "拖动" in html or "拼图" in html:
            return "slider"
        if "header-answer" in html:
            return "click"
        if "依次点击" in html or "请点击" in html or "点击下图" in html:
            return "click"
    except Exception:
        pass
    return "unknown"


def _wait_for_captcha(page: Page, selectors: dict, timeout: int) -> bool:
    for sel in _WIDGET_SELECTORS:
        try:
            page.wait_for_selector(sel, state="attached", timeout=timeout * 1000)
            return True
        except Exception:
            continue
    return False


def _find_element(page: Page, selector: str, wait: float = 1.0) -> Optional[ElementHandle]:
    try:
        return page.wait_for_selector(selector, state="attached", timeout=wait * 1000)
    except Exception:
        return None


def _find_main_image_element(page: Page, selectors: dict,
                             expected_aspect: float = None) -> Optional[ElementHandle]:
    """找到用于坐标转换的可见图片元素。优先宽高比与主图匹配的元素。"""
    best = None
    best_aspect_diff = float('inf')
    
    for sel in [
        selectors.get("verify_bg_img"),
        selectors.get("image_area"),
        selectors.get("point_area"),
        selectors.get("click_type_wrap"),
        selectors.get("verify_bg"),
    ]:
        els = page.query_selector_all(sel)
        for el in els:
            try:
                if not el.is_visible():
                    continue
                rect = el.bounding_box()
                if not rect or rect["width"] < 80 or rect["height"] < 80:
                    continue
                    
                if expected_aspect:
                    aspect = rect["width"] / rect["height"]
                    diff = abs(aspect - expected_aspect) / expected_aspect
                    if diff < best_aspect_diff:
                        best_aspect_diff = diff
                        best = el
                else:
                    best = el
                    break
            except Exception:
                pass
        if best and not expected_aspect:
            break
            
    return best


def _click_with_actions(page: Page, element: ElementHandle,
                        offset_x: float, offset_y: float):
    """使用 Playwright 点击元素特定偏移。"""
    try:
        box = element.bounding_box()
        if not box:
            return
        
        # 偏移相对于元素中心
        click_x = box['x'] + box['width'] / 2 + offset_x
        click_y = box['y'] + box['height'] / 2 + offset_y
        
        page.mouse.move(click_x, click_y)
        time.sleep(random.uniform(0.05, 0.15))
        page.mouse.click(click_x, click_y)
    except Exception as e:
        logger.error(f"点击偏移错误: {e}")
        # 回退到 JS 点击
        try:
            page.evaluate("""
                ({el, ox, oy}) => {
                    var r = el.getBoundingClientRect();
                    var cx = r.left + r.width/2 + ox;
                    var cy = r.top + r.height/2 + oy;
                    var target = document.elementFromPoint(cx, cy);
                    if (target) {
                        ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(t){
                            target.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, clientX:cx, clientY:cy}));
                        });
                    }
                }
            """, {"el": element, "ox": offset_x, "oy": offset_y})
        except Exception:
            pass


def _check_passed(page: Page, selectors: dict) -> bool:
    from urllib.parse import urlparse
    if "/login" not in urlparse(page.url).path:
        return True
    try:
        body_text = page.inner_text("body") or ""
        if any(kw in body_text for kw in ["登录成功", "验证成功", "success"]):
            return True
    except Exception:
        pass
    
    el = _find_element(page, selectors["content"], wait=0.5)
    if el is None or not el.is_visible():
        time.sleep(2)
        el2 = _find_element(page, selectors["content"], wait=0.5)
        if el2 is None or not el2.is_visible():
            return True
    return False


def _refresh_captcha(page: Page, selectors: dict):
    """刷新验证码。"""
    # 策略 1: 标准刷新按钮
    btn = _find_element(page, selectors.get("refresh_btn"), wait=0.5)
    if btn is not None and btn.is_visible():
        try:
            btn.click()
            logger.info("已点击刷新按钮")
            return
        except Exception:
            pass

    # 策略 2: footer icon 区域的 img
    refresh_div = _find_element(page, ".tencent-captcha-dy__footer-icon--refresh", wait=0.5)
    if refresh_div is not None:
        try:
            img = refresh_div.query_selector("img")
            if img and img.is_visible():
                img.click()
                logger.info("已点击刷新图片")
                return
        except Exception:
            pass

    # 策略 3: JS 回退
    try:
        page.evaluate("""
            () => {
                var els = document.querySelectorAll('[class*="refresh"], [class*="footer-icon"]');
                for (var i = 0; i < els.length; i++) {
                    if (els[i].offsetParent !== null && els[i].getBoundingClientRect().width > 5) {
                        els[i].click();
                        return;
                    }
                }
            }
        """)
        logger.info("已通过JS回退方式点击刷新")
    except Exception:
        pass
