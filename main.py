#!/usr/bin/env python3
"""
FavsSnap — 微信公众号文章剪藏工具

将微信公号文章转换为 本地Markdown + 本地图片，支持批量剪藏。
"""
import os, re, sys, hashlib, threading, ctypes
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlparse, urljoin
import requests, chardet
from bs4 import BeautifulSoup
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext


# ==================== 工具核心 ====================

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}


def fetch_article(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        raw = resp.content
        detected = chardet.detect(raw)
        enc = detected.get('encoding') or 'utf-8'
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            return raw.decode('utf-8', errors='replace')
    except Exception:
        return None


def extract_article_content(html: str, url: str) -> Dict:
    soup = BeautifulSoup(html, 'lxml')

    title = ""
    title_tag = soup.find('h1', class_='rich_media_title') or soup.find('h2', class_='rich_media_title')
    if title_tag:
        title = title_tag.get_text(strip=True)
    else:
        meta_title = soup.find('meta', property='og:title')
        if meta_title:
            title = meta_title.get('content', '')
    if not title:
        title = f"微信文章 - {url}"

    content_html = ""
    content_div = soup.find('div', id='js_content') or soup.find('div', class_='rich_media_content')
    if content_div:
        content_html = str(content_div)
    else:
        for div in soup.find_all('div', class_='rich_media_area_primary'):
            content_div = div.find('section')
            if content_div:
                content_html = str(content_div)
                break

    # 检测贴图文章：js_content 为空 + 存在 content_noencode
    is_slideshow = False
    slideshow_text = ""
    slideshow_images = []
    if not content_html or len(BeautifulSoup(content_html, 'lxml').get_text(strip=True)) < 20:
        m = re.search(r"content_noencode:\s*'((?:[^'\\]|\\.)*)'", html, re.DOTALL)
        if m:
            raw_text = m.group(1)
            raw_text = raw_text.replace('\\x0a', '\n').replace('\\n', '\n')
            raw_text = raw_text.replace('\\x3c', '<').replace('\\x3e', '>')
            raw_text = raw_text.replace('\\x22', '"').replace('\\x26', '&')
            # 清理 HTML 标签
            text_soup = BeautifulSoup(raw_text, 'lxml')
            slideshow_text = text_soup.get_text('\n', strip=True)
            if len(slideshow_text) > 20:
                is_slideshow = True
                # 从 window.picture_page_info_list 提取幻灯片图片
                pp_idx = html.find('window.picture_page_info_list')
                if pp_idx >= 0:
                    arr_start = html.index('[', pp_idx)
                    depth = 0
                    arr_end = arr_start
                    for ci in range(arr_start, min(arr_start + 100000, len(html))):
                        if html[ci] == '[':
                            depth += 1
                        elif html[ci] == ']':
                            depth -= 1
                            if depth == 0:
                                arr_end = ci + 1
                                break
                    arr_str = html[arr_start:arr_end]
                    # 匹配顶层 width, height, cdn_url
                    pages = re.findall(
                        r"width:\s*'(\d+)'\s*\*\s*1,\s*height:\s*'(\d+)'\s*\*\s*1,\s*cdn_url:\s*'([^']*)'",
                        arr_str)
                    for _, _, url in pages:
                        url = url.replace('\\x26amp;', '&')
                        if url:
                            slideshow_images.append(url)

    # 优先从 nick_name 字段提取作者
    author = ""
    nick_m = re.search(r"nick_name:\s*'((?:[^'\\]|\\.)*)'", html)
    if nick_m:
        author = nick_m.group(1).replace('\\x26', '&').strip()
    if not author:
        author_tag = soup.find('span', class_='rich_media_meta_nickname')
        if author_tag:
            author = author_tag.get_text(strip=True)

    publish_date = ""
    date_tag = soup.find('em', class_='rich_media_meta_text')
    if date_tag:
        publish_date = date_tag.get_text(strip=True)

    return {
        'title': title,
        'author': author,
        'publish_date': publish_date,
        'content_html': content_html,
        'raw_html': html,
        'is_slideshow': is_slideshow,
        'slideshow_text': slideshow_text,
        'slideshow_images': slideshow_images,
    }


def html_to_markdown(content_html: str, url: str, image_mapping: dict) -> str:
    if not content_html:
        return ""

    soup = BeautifulSoup(content_html, 'lxml')

    def block_join(parts):
        """Join child results: space between inline elements, no space before block newlines."""
        result = []
        for r in parts:
            if result:
                prev = result[-1]
                if prev.endswith('\n'):
                    if re.match(r'^(- |\d+\.)', r):
                        # Consecutive list items: single newline
                        pass
                    else:
                        # Block to non-list: extra newline for paragraph break
                        result.append('\n')
                elif not r.startswith('\n'):
                    result.append(' ')
            result.append(r)
        return ''.join(result)

    def process_element(element):
        if isinstance(element, str):
            text = element.strip()
            return text if text else None

        if element.name == 'img':
            src = element.get('src') or element.get('data-src')
            if src:
                full_url = urljoin(url, src)
                if full_url in image_mapping:
                    alt = element.get('alt', '')
                    return f"![{alt}]({image_mapping[full_url]})"
            return None

        if element.name == 'pre':
            code = element.find('code')
            if code:
                lines = []
                for child in code.children:
                    if isinstance(child, str):
                        t = child.strip()
                        if t:
                            lines.append(t)
                    elif child.name == 'br':
                        lines.append('')
                    else:
                        t = child.get_text(strip=True)
                        if t:
                            lines.append(t)
                code_text = '\n'.join(lines)
                return f'\n```\n{code_text}\n```\n\n'
            return None

        if element.name == 'code':
            text = element.get_text(strip=True)
            return f'`{text}`' if text else None

        if element.name in ['strong', 'b']:
            text = element.get_text(strip=True)
            return f'**{text}**' if text else None

        if element.name in ['em', 'i']:
            text = element.get_text(strip=True)
            return f'*{text}*' if text else None

        if element.name in ['p', 'section', 'div']:
            mpa_key = element.get('data-mpa-md-key', '')

            if mpa_key.startswith('heading-') and mpa_key[-1].isdigit():
                level = int(mpa_key[-1])
                text = element.get_text(strip=True)
                return f"{'#' * level} {text}\n\n" if text else None

            if mpa_key == 'blockquote':
                text = element.get_text(strip=True)
                return f'> {text}\n\n' if text else None

            if mpa_key in ('bullet-list', 'ordered-list'):
                return None

            parts = [r for r in (process_element(c) for c in element.children) if r]
            if not parts:
                return None
            return block_join(parts) + '\n\n'

        if element.name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            level = int(element.name[1])
            text = element.get_text(strip=True)
            return f"{'#' * level} {text}\n\n" if text else None

        if element.name in ['ul', 'ol']:
            items = []
            for idx, li in enumerate(element.find_all('li', recursive=False), 1):
                t = li.get_text(strip=True)
                if t:
                    items.append(f"{idx}. {t}" if element.name == 'ol' else f"- {t}")
            return '\n'.join(items) + '\n' if items else None

        if element.name == 'blockquote':
            lines = element.get_text(strip=False).splitlines()
            return '\n'.join(f'> {line}' for line in lines) + '\n\n'

        if element.name == 'br':
            return "\n\n"

        if element.name == 'hr':
            return "\n---\n\n"

        if element.name in ('svg', 'style', 'script', 'mp-common-profile', 'mp-style-type', 'template', 'ellipse'):
            return None

        parts = [r for r in (process_element(c) for c in element.children) if r]
        return block_join(parts) if parts else None

    markdown_parts = []
    for element in soup.children:
        result = process_element(element)
        if result:
            markdown_parts.append(result)

    result = ''.join(markdown_parts)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def get_image_filename(url: str, index: int, article_index: int = 0) -> str:
    parsed = urlparse(url)
    ext = os.path.splitext(parsed.path)[1]
    if not ext or len(ext) > 5:
        ext = '.jpg'
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    prefix = f"A{article_index:02d}_" if article_index else ""
    return f"img_{prefix}{index:03d}_{url_hash}{ext}"


def download_image(url: str, save_path: str, timeout: int = 10) -> bool:
    headers = {**HEADERS, 'Referer': 'https://mp.weixin.qq.com/'}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
        resp.raise_for_status()
        with open(save_path, 'wb') as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        return True
    except Exception:
        return False


def extract_images(html_content: str, base_url: str) -> List[Tuple[str, any]]:
    soup = BeautifulSoup(html_content, 'lxml')
    images = []
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src')
        if src:
            images.append((urljoin(base_url, src), img))
    return images


def save_images(images: List[Tuple[str, any]], output_dir: str, article_index: int = 0) -> dict:
    images_dir = os.path.join(output_dir, 'images')
    os.makedirs(images_dir, exist_ok=True)
    url_mapping = {}
    for idx, (img_url, _) in enumerate(images, 1):
        if img_url in url_mapping:
            continue
        filename = get_image_filename(img_url, idx, article_index)
        save_path = os.path.join(images_dir, filename)
        if download_image(img_url, save_path):
            url_mapping[img_url] = f"images/{filename}"
    return url_mapping


def scrape_one(url: str, output_dir: str, log, index: int = 0, total: int = 0) -> bool:
    header = f"[{index}/{total}]" if total else ""
    log(f"{header}  抓取：{url}", 'dim')
    html = fetch_article(url)
    if not html:
        log(f"{'  ' * 2}抓取失败", 'error')
        return False

    article = extract_article_content(html, url)

    if article['is_slideshow']:
        # 贴图文章：文字 + 幻灯片图片
        slideshow_imgs = article['slideshow_images']
        img_tuples = [(u, None) for u in slideshow_imgs]
        image_mapping = save_images(img_tuples, output_dir, index) if img_tuples else {}

        # 构建 Markdown：文字 + 图片
        md_parts = []
        text = article['slideshow_text'].strip()
        if text:
            md_parts.append(text)
        for img_url in slideshow_imgs:
            if img_url in image_mapping:
                md_parts.append(f"\n![图片]({image_mapping[img_url]})")
        md = '\n\n'.join(md_parts)

        images = img_tuples
    else:
        # 普通文章：只从正文区域提取图片
        images = extract_images(article['content_html'], url)
        image_mapping = save_images(images, output_dir, index) if images else {}
        md = html_to_markdown(article['content_html'], url, image_mapping)

    frontmatter = [f"# {article['title']}", ""]
    if article['author']:
        frontmatter.append(f"**作者**: {article['author']}")
    if article['publish_date']:
        frontmatter.append(f"**发布时间**: {article['publish_date']}")
    frontmatter += [
        f"**原文链接**: {url}",
        f"**抓取时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "", "---", "",
    ]
    final_content = '\n'.join(frontmatter) + md

    safe_title = article['title'][:50].replace('/', '_').replace('\\', '_')
    safe_title = "".join(c for c in safe_title if c.isalnum() or c in ' -_').strip()
    if not safe_title:
        safe_title = "wechat-article"

    md_path = os.path.join(output_dir, f"{safe_title}.md")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(final_content)

    author = article['author'] or '—'
    article_type = "贴图文章" if article['is_slideshow'] else "普通文章"
    log(f"{'  ' * 2}标题：{article['title']}")
    log(f"{'  ' * 2}作者：{author}{' ' * 10}图片：{len(images)} 张  类型：{article_type}")
    log(f"{'  ' * 2}保存：{md_path}", 'success')
    return True


# ==================== GUI ====================

DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')

# 薄荷绿知识卡片风格配色 — Claude Design 原则
COLORS = {
    'bg': '#f6faf7',              # 主背景 暖调薄荷绿画布
    'surface': '#ffffff',          # 卡片背景 白
    'surface2': '#f1f8f3',         # 输入框背景 浅绿卡片色
    'surface_dark': '#1b2d25',     # 深绿表面（日志区色块深度）
    'border': '#d4e8db',           # 边框 柔和发丝线
    'border_light': '#e3efe6',     # 浅边框
    'text': '#1a1f1c',             # 主文字 暖深色
    'text_mid': '#3b423d',         # 中等文字
    'text_dim': '#6a736c',         # 次要文字
    'text_soft': '#8c948b',        # 柔和文字
    'accent': '#3bb88a',           # 主色 松石绿
    'accent_hover': '#2ea077',     # 主色悬停
    'accent_light': '#dbf5e8',     # 主色浅底
    'green': '#2d8c5a',            # 成功
    'red': '#d44a4a',              # 失败
    'yellow': '#c8921e',           # 警告
    'blue': '#4a90d9',             # 信息
    'peach': '#d47a3c',            # 高亮
}


def setup_styles():
    style = ttk.Style()
    style.theme_use('clam')

    style.configure('.', background=COLORS['bg'], foreground=COLORS['text'],
                    fieldbackground=COLORS['surface'], bordercolor=COLORS['border'],
                    insertcolor=COLORS['text'], font=('Microsoft YaHei UI', 10))

    style.configure('TFrame', background=COLORS['bg'])
    style.configure('Card.TFrame', background=COLORS['surface'],
                    relief='flat', borderwidth=1)

    style.configure('TLabel', background=COLORS['bg'], foreground=COLORS['text'],
                    font=('Microsoft YaHei UI', 10))
    style.configure('Card.TLabel', background=COLORS['surface'])
    style.configure('Dim.TLabel', foreground=COLORS['text_soft'], font=('Microsoft YaHei UI', 9))
    style.configure('Mid.TLabel', foreground=COLORS['text_mid'], font=('Microsoft YaHei UI', 10))
    style.configure('Title.TLabel', font=('Microsoft YaHei UI', 16, 'bold'),
                    foreground=COLORS['accent'])
    style.configure('Sub.TLabel', font=('Microsoft YaHei UI', 9),
                    foreground=COLORS['text_soft'])
    style.configure('Section.TLabel', font=('Microsoft YaHei UI', 10, 'bold'),
                    foreground=COLORS['text_mid'], background=COLORS['surface'])

    style.configure('TEntry', fieldbackground=COLORS['surface2'],
                    bordercolor=COLORS['border'], insertcolor=COLORS['text'],
                    lightcolor=COLORS['border_light'])
    style.map('TEntry',
              bordercolor=[('focus', COLORS['accent'])],
              lightcolor=[('focus', COLORS['accent_light'])])

    style.configure('Accent.TButton', background=COLORS['accent'],
                    foreground='#ffffff', font=('Microsoft YaHei UI', 10, 'bold'),
                    padding=(22, 8), relief='flat')
    style.map('Accent.TButton',
              background=[('active', COLORS['accent_hover']),
                          ('disabled', COLORS['border_light'])],
              foreground=[('disabled', COLORS['text_soft'])])

    style.configure('TButton', background=COLORS['surface'],
                    foreground=COLORS['text_mid'], font=('Microsoft YaHei UI', 9),
                    padding=(12, 5), relief='flat',
                    bordercolor=COLORS['border'])
    style.map('TButton',
              background=[('active', COLORS['accent_light'])],
              bordercolor=[('focus', COLORS['accent'])])

    style.configure('Horizontal.TProgressbar',
                    background=COLORS['accent'],
                    troughcolor=COLORS['surface2'],
                    bordercolor=COLORS['border_light'],
                    lightcolor=COLORS['accent'],
                    darkcolor=COLORS['accent'],
                    thickness=5)

    style.configure('CardTitle.TFrame', background=COLORS['surface'])
    style.configure('CardTitle.TLabel', background=COLORS['surface'],
                    foreground=COLORS['text'], font=('Microsoft YaHei UI', 10, 'bold'))
    style.configure('CardDim.TLabel', background=COLORS['surface'],
                    foreground=COLORS['text_soft'], font=('Microsoft YaHei UI', 9))


class RoundedButton(tk.Canvas):
    """Canvas 实现的圆角按钮"""

    def __init__(self, parent, text, command=None, radius=12,
                 bg=None, fg='#ffffff', hover_bg=None, font=None, padx=20, pady=8, **kwargs):
        self.bg = bg or COLORS['accent']
        self.hover_bg = hover_bg or COLORS['accent_hover']
        self.fg = fg
        self.radius = radius
        self.command = command
        self._text = text
        self._padx = padx
        self._pady = pady
        self._font = font or ('Microsoft YaHei UI', 10, 'bold')
        self._enabled = True

        # 计算尺寸
        import tkinter.font as tkfont
        f = tkfont.Font(family=self._font[0], size=self._font[1],
                        weight=self._font[2] if len(self._font) > 2 else 'normal')
        tw = f.measure(text)
        th = f.metrics('linespace')
        w = tw + padx * 2
        h = th + pady * 2

        try:
            parent_bg = parent['bg']
        except (tk.TclError, KeyError):
            parent_bg = COLORS['bg']
        super().__init__(parent, width=w, height=h,
                         bg=parent_bg, highlightthickness=0, bd=0, **kwargs)

        self._draw(self.bg)
        self.bind('<Enter>', self._on_enter)
        self.bind('<Leave>', self._on_leave)
        self.bind('<ButtonRelease-1>', self._on_click)

    def _draw(self, color):
        self.delete('all')
        w = int(self['width'])
        h = int(self['height'])
        r = self.radius
        points = [
            r, 0, w - r, 0, w, 0, w, r,
            w, h - r, w, h, w - r, h, r, h,
            0, h, 0, h - r, 0, r, 0, 0, r, 0,
        ]
        self.create_polygon(points, smooth=True, fill=color, outline=color)
        self.create_text(w // 2, h // 2, text=self._text,
                         fill=self.fg, font=self._font)

    def _on_enter(self, e):
        if self._enabled:
            self._draw(self.hover_bg)

    def _on_leave(self, e):
        if self._enabled:
            self._draw(self.bg)

    def _on_click(self, e):
        if self._enabled and self.command:
            self.command()

    def config_state(self, state):
        self._enabled = state == 'normal'
        if not self._enabled:
            self._draw(COLORS['border_light'])
            self.configure(cursor='')
        else:
            self._draw(self.bg)
            self.configure(cursor='hand2')

    def set_bg(self, bg, hover_bg=None):
        self.bg = bg
        if hover_bg:
            self.hover_bg = hover_bg
        self._draw(self.bg)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("FavsSnap — 微信文章剪藏")
        self.root.geometry("780x600")
        self.root.minsize(620, 480)
        self.root.configure(bg=COLORS['bg'])
        self.root.resizable(True, True)

        self.running = False
        self.placeholder = "在此粘贴文章链接，支持批量（每行一条）"

        setup_styles()

        # --- 主容器 ---
        main = ttk.Frame(root)
        main.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        # --- 顶部装饰条 ---
        self._gradient_bar(main)

        # --- 标题区（居中） ---
        frm_header = ttk.Frame(main)
        frm_header.pack(fill=tk.X, pady=(12, 16))
        ttk.Label(frm_header, text="FavsSnap",
                  style='Title.TLabel').pack(anchor=tk.CENTER)
        ttk.Label(frm_header, text="将微信文章剪藏为 本地Markdown + 本地图片",
                  style='Sub.TLabel').pack(anchor=tk.CENTER, pady=(3, 0))

        # --- 链接输入卡片 ---
        card_url = self._make_card(main, expand=True)

        frm_url_label = ttk.Frame(card_url, style='CardTitle.TFrame')
        frm_url_label.pack(fill=tk.X)
        ttk.Label(frm_url_label, text="文章链接", style='CardTitle.TLabel').pack(side=tk.LEFT)
        ttk.Label(frm_url_label, text="每行一条", style='CardDim.TLabel').pack(side=tk.LEFT, padx=(8, 0))

        self.txt_urls = tk.Text(card_url, height=3, wrap=tk.WORD,
                                bg=COLORS['surface'], fg=COLORS['text'],
                                insertbackground=COLORS['accent'],
                                selectbackground=COLORS['accent_light'],
                                selectforeground=COLORS['text'],
                                relief=tk.FLAT, bd=0,
                                highlightthickness=1,
                                highlightbackground=COLORS['border'],
                                highlightcolor=COLORS['accent'],
                                font=('Microsoft YaHei UI', 10),
                                padx=10, pady=8)
        self.txt_urls.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.txt_urls.insert("1.0", self.placeholder)
        self.txt_urls.config(fg=COLORS['text_dim'])
        self.txt_urls.bind('<FocusIn>', self._on_focus_in)
        self.txt_urls.bind('<FocusOut>', self._on_focus_out)

        # --- 输出目录 + 操作栏（同一行） ---
        frm_bottom = ttk.Frame(main)
        frm_bottom.pack(fill=tk.X, pady=(0, 12))

        frm_dir = ttk.Frame(frm_bottom)
        frm_dir.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(frm_dir, text="输出目录", style='Mid.TLabel').pack(side=tk.LEFT)
        self.var_dir = tk.StringVar(value=DEFAULT_OUTPUT)
        dir_entry = ttk.Entry(frm_dir, textvariable=self.var_dir)
        dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 6))
        RoundedButton(frm_dir, "浏览", command=self.browse_dir,
                      bg=COLORS['surface2'], hover_bg=COLORS['accent_light'],
                      fg=COLORS['text_mid'], font=('Microsoft YaHei UI', 9),
                      padx=14, pady=5).pack(side=tk.LEFT)

        frm_action = ttk.Frame(frm_bottom)
        frm_action.pack(side=tk.RIGHT, padx=(12, 0))
        self.btn_start = RoundedButton(frm_action, "开始抓取", command=self.start,
                                       bg=COLORS['accent'], hover_bg=COLORS['accent_hover'],
                                       fg='#ffffff',
                                       font=('Microsoft YaHei UI', 10),
                                       padx=22, pady=8)
        self.btn_start.pack(side=tk.RIGHT)

        self.lbl_status = ttk.Label(frm_action, text="就绪", style='Dim.TLabel')
        self.lbl_status.pack(side=tk.RIGHT, padx=(0, 12))

        # --- 日志卡片 ---
        card_log = self._make_card(main, expand=True)

        # 标题行：运行日志 + 进度条 + 百分比
        frm_log_header = ttk.Frame(card_log, style='CardTitle.TFrame')
        frm_log_header.pack(fill=tk.X)

        ttk.Label(frm_log_header, text="运行日志", style='CardTitle.TLabel').pack(side=tk.LEFT)

        self.frm_prog = ttk.Frame(frm_log_header, style='CardTitle.TFrame')
        self.progress = ttk.Progressbar(self.frm_prog, mode='determinate', length=120)
        self.progress.pack(side=tk.LEFT, padx=(12, 6))
        self.lbl_percent = ttk.Label(self.frm_prog, text="0%", style='CardDim.TLabel')
        self.lbl_percent.pack(side=tk.LEFT)
        # 初始隐藏，运行时显示

        self.txt_log = tk.Text(card_log, height=8, wrap=tk.WORD,
                               bg=COLORS['surface_dark'], fg='#8e9b93',
                               relief=tk.FLAT, bd=0,
                               highlightthickness=1,
                               highlightbackground=COLORS['surface_dark'],
                               highlightcolor=COLORS['surface_dark'],
                               font=('Consolas', 9),
                               padx=10, pady=8,
                               state=tk.DISABLED,
                               cursor='arrow')
        self.txt_log.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        # 日志颜色标签（深色背景适配）
        self.txt_log.tag_configure('info', foreground='#7bc8e0')
        self.txt_log.tag_configure('success', foreground='#6dd49e')
        self.txt_log.tag_configure('error', foreground='#f2776e')
        self.txt_log.tag_configure('warn', foreground='#e8c96a')
        self.txt_log.tag_configure('highlight', foreground='#f2ae72')
        self.txt_log.tag_configure('dim', foreground='#6a726a')

    def _rounded_rect(self, canvas, x1, y1, x2, y2, radius, fill, outline, tag=''):
        """在 Canvas 上绘制圆角矩形"""
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1, x2, y1 + radius,
            x2, y2 - radius,
            x2, y2, x2 - radius, y2,
            x1 + radius, y2,
            x1, y2, x1, y2 - radius,
            x1, y1 + radius,
            x1, y1, x1 + radius, y1,
        ]
        return canvas.create_polygon(points, smooth=True, fill=fill, outline=outline, tags=tag)

    def _make_card(self, parent, expand=False):
        """创建圆角白色卡片容器"""
        R = 14  # 圆角半径
        PAD = 10  # 卡片外边距

        # 外层 Canvas 用于绘制圆角背景
        canvas = tk.Canvas(parent, bg=COLORS['bg'], highlightthickness=0, bd=0)
        pack_opts = {'fill': tk.BOTH, 'pady': (0, PAD)}
        if expand:
            pack_opts['expand'] = True
        canvas.pack(**pack_opts)

        # 内容 Frame 放在 Canvas 内部
        inner = tk.Frame(canvas, bg=COLORS['surface'], padx=18, pady=16)
        inner_id = canvas.create_window(0, 0, window=inner, anchor='nw')

        def _resize(event):
            w, h = event.width, event.height
            canvas.delete('bg')
            self._rounded_rect(canvas, 1, 1, w - 2, h - 2, R,
                               fill=COLORS['surface'], outline=COLORS['border'], tag='bg')
            canvas.tag_lower('bg')
            canvas.coords(inner_id, 18, 16)
            canvas.itemconfigure(inner_id, width=w - 36, height=h - 32)

        canvas.bind('<Configure>', _resize)
        return inner

    def _gradient_bar(self, parent):
        """顶部渐变装饰条"""
        bar = tk.Canvas(parent, height=4, highlightthickness=0, bd=0)
        bar.pack(fill=tk.X, pady=(0, 4))
        colors = ['#3bb88a', '#5bc49c', '#7ed4b2', '#a6e3ca']
        w = 800
        seg = w // len(colors)
        for i, c in enumerate(colors):
            bar.create_rectangle(i * seg, 0, (i + 1) * seg, 4, fill=c, outline='')
        bar.configure(bg=COLORS['bg'])

    def _on_focus_in(self, event):
        if self.txt_urls.get("1.0", tk.END).strip() == self.placeholder:
            self.txt_urls.delete("1.0", tk.END)
            self.txt_urls.config(fg=COLORS['text'])

    def _on_focus_out(self, event):
        if not self.txt_urls.get("1.0", tk.END).strip():
            self.txt_urls.insert("1.0", self.placeholder)
            self.txt_urls.config(fg=COLORS['text_dim'])

    def browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_dir.get())
        if d:
            self.var_dir.set(d)

    def log(self, msg: str, tag: str = ''):
        self.txt_log.config(state=tk.NORMAL)
        if tag:
            self.txt_log.insert(tk.END, msg + "\n", tag)
        else:
            self.txt_log.insert(tk.END, msg + "\n")
        self.txt_log.see(tk.END)
        self.txt_log.config(state=tk.DISABLED)

    def _get_urls(self):
        raw = self.txt_urls.get("1.0", tk.END).strip()
        if raw == self.placeholder:
            return []
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def start(self):
        urls = self._get_urls()
        if not urls:
            self.log("请先输入文章链接", 'warn')
            return

        output_dir = self.var_dir.get().strip() or DEFAULT_OUTPUT
        os.makedirs(output_dir, exist_ok=True)

        self.running = True
        self.btn_start.config_state('disabled')
        self.progress['value'] = 0
        self.progress['maximum'] = len(urls)
        self.lbl_percent.config(text="0%")
        self.frm_prog.pack(side=tk.RIGHT)
        self.lbl_status.config(text=f"正在抓取 0/{len(urls)}")

        threading.Thread(target=self.run, args=(urls, output_dir), daemon=True).start()

    def run(self, urls: list, output_dir: str):
        total = len(urls)
        ok = 0
        fail = 0

        for i, url in enumerate(urls, 1):
            def log_msg(m, tag=''):
                self.root.after(0, self.log, m, tag)
            try:
                if scrape_one(url, output_dir, log_msg, index=i, total=total):
                    ok += 1
                    pct = f"{int(i / total * 100)}%"
                    self.root.after(0, self.progress.config, {'value': i})
                    self.root.after(0, self.lbl_percent.config, {'text': pct})
                    self.root.after(0, self.lbl_status.config,
                                    {'text': f"正在抓取 {i}/{total}"})
                else:
                    fail += 1
            except Exception as e:
                self.root.after(0, self.log, f"  异常：{e}", 'error')
                fail += 1

        def finish():
            tag = 'success' if fail == 0 else 'warn'
            self.log(f"\n完成 — 成功 {ok}，失败 {fail}", tag)
            self.lbl_status.config(text=f"完成 — 成功 {ok}，失败 {fail}")
            self.progress['value'] = total
            self.lbl_percent.config(text="100%")
            self.btn_start.config_state('normal')
            self.running = False
            # 延迟隐藏进度条
            self.root.after(2000, self.frm_prog.pack_forget)

        self.root.after(0, finish)


def main():
    # 设置 Windows 任务栏图标
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('FavsSnap')

    root = tk.Tk()

    # 设置窗口图标
    base_dir = os.path.dirname(os.path.abspath(__file__))
    logo_path = os.path.join(base_dir, 'logo.png')
    if os.path.exists(logo_path):
        try:
            img = tk.PhotoImage(file=logo_path)
            root.iconphoto(False, img)
        except Exception:
            pass

    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
