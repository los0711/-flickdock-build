"""Translation targets and local-only clipboard eligibility."""
import re
import sys
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

MAX_TEXT_CHARS = 16000
DETECTION_SAMPLE_CHARS = 1000
DEFAULT_TARGET = 'zh'
DETECTING_SOURCE = '识别中'
UNKNOWN_SOURCE = '未识别语言'
# Stable language codes are stored in preferences; region codes only select artwork.
LANGUAGES = (
    ('zh', '简体中文', 'cn'), ('zh-Hant', '繁体中文', 'tw'),
    ('en', '英语', 'us'), ('ja', '日语', 'jp'), ('ko', '韩语', 'kr'),
    ('fr', '法语', 'fr'), ('de', '德语', 'de'), ('es', '西班牙语', 'es'),
    ('pt', '葡萄牙语', 'pt'), ('ru', '俄语', 'ru'), ('it', '意大利语', 'it'),
    ('nl', '荷兰语', 'nl'), ('ar', '阿拉伯语', 'sa'), ('th', '泰语', 'th'),
    ('vi', '越南语', 'vn'), ('id', '印尼语', 'id'), ('hi', '印地语', 'in'),
    ('tr', '土耳其语', 'tr'), ('pl', '波兰语', 'pl'), ('sv', '瑞典语', 'se'),
    ('da', '丹麦语', 'dk'), ('nb', '挪威语', 'no'), ('fi', '芬兰语', 'fi'),
    ('el', '希腊语', 'gr'), ('cs', '捷克语', 'cz'), ('he', '希伯来语', 'il'),
    ('uk', '乌克兰语', 'ua'), ('ro', '罗马尼亚语', 'ro'),
    ('ms', '马来语', 'my'), ('fil', '菲律宾语', 'ph'),
    ('yue', '粤语', ''), ('af', '南非荷兰语', ''), ('am', '阿姆哈拉语', ''),
    ('az', '阿塞拜疆语', ''), ('be', '白俄罗斯语', ''), ('bg', '保加利亚语', ''),
    ('bn', '孟加拉语', ''), ('bs', '波斯尼亚语', ''), ('ca', '加泰罗尼亚语', ''),
    ('cy', '威尔士语', ''), ('et', '爱沙尼亚语', ''), ('eu', '巴斯克语', ''),
    ('fa', '波斯语', ''), ('ga', '爱尔兰语', ''), ('gl', '加利西亚语', ''),
    ('gu', '古吉拉特语', ''), ('hr', '克罗地亚语', ''), ('hu', '匈牙利语', ''),
    ('hy', '亚美尼亚语', ''), ('is', '冰岛语', ''), ('ka', '格鲁吉亚语', ''),
    ('kk', '哈萨克语', ''), ('km', '高棉语', ''), ('kn', '卡纳达语', ''),
    ('ku', '库尔德语', ''), ('lo', '老挝语', ''), ('lt', '立陶宛语', ''),
    ('lv', '拉脱维亚语', ''), ('mk', '马其顿语', ''), ('ml', '马拉雅拉姆语', ''),
    ('mn', '蒙古语', ''), ('mr', '马拉地语', ''), ('my', '缅甸语', ''),
    ('ne', '尼泊尔语', ''), ('or', '奥里亚语', ''), ('pa', '旁遮普语', ''),
    ('si', '僧伽罗语', ''), ('sk', '斯洛伐克语', ''), ('sl', '斯洛文尼亚语', ''),
    ('sq', '阿尔巴尼亚语', ''), ('sr', '塞尔维亚语', ''), ('sw', '斯瓦希里语', ''),
    ('ta', '泰米尔语', ''), ('te', '泰卢固语', ''), ('ur', '乌尔都语', ''),
    ('uz', '乌兹别克语', ''), ('yo', '约鲁巴语', ''), ('zu', '祖鲁语', ''),
)
TARGETS = {code: (label, flag) for code, label, flag in LANGUAGES}
# Match the desktop-facing codes accepted by the BreezeBlue proxy, including
# its zh-Hant / nb / fil aliases. Text translation has a broader catalog.
SPEECH_LANGUAGES = frozenset(
    'af ar bg bn bs ca cs da de el en es et eu fa fi fr gl he hi hu id is it ja kn ko lt mr ms '
    'ne nl nb pl pt ro ru sk sl sq sv sw ta te th fil tr uk ur vi zh zh-Hant'.split()
)
SOURCE_NAMES = {code: label for code, label, _ in LANGUAGES}
SOURCE_NAMES.update(zh='中文', no='挪威语', tl='菲律宾语')
URI_SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
BARE_DOMAIN = re.compile(r'^(?:[\w-]+\.)+[A-Za-z\u0080-\uffff]{2,63}(?::\d{1,5})?(?:[/?#].*)?$', re.UNICODE)


def is_link_line(text):
    """Do not invoke video recognition or resolve DNS to classify a copied URL."""
    text = text.strip().strip('<>')
    if not text or any(char.isspace() for char in text):
        return False
    if URI_SCHEME.match(text):
        # Includes non-web links such as mailto:, magnet: and application URLs.
        return True
    if text.lower().startswith('www.') or BARE_DOMAIN.fullmatch(text):
        try:
            return bool(urlsplit('https://' + text).hostname)
        except ValueError:
            return False
    return False


def copied_text(value):
    """Only local, bounded prose can create a translation invitation."""
    if not isinstance(value, (tuple, list)) or len(value) != 2 or value[0] != 'text' or not isinstance(value[1], dict):
        return None
    text = value[1].get('text')
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > MAX_TEXT_CHARS or '\x00' in text or not any(c.isalpha() for c in text):
        return None
    if all(is_link_line(part) for part in text.split()):
        return None
    return text


@cache
def _detector():
    import fasttext
    root=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))
    return fasttext.load_model(str(root/'assets'/'models'/'lid.176.ftz'))


def detect_source(text):
    """Use the bundled fastText model locally on the translation worker."""
    sample=' '.join(str(text)[:DETECTION_SAMPLE_CHARS].split())
    if sum(char.isalpha() for char in sample)<2:return UNKNOWN_SOURCE
    labels,_=_detector().predict(sample,k=1)
    code=labels[0].removeprefix('__label__') if labels else ''
    # Short phrases often have a low score even when the best result is right.
    # Translation itself independently auto-detects the input language.
    return SOURCE_NAMES.get(code, UNKNOWN_SOURCE)


# An AI polish must keep the language of the text it rewrites. The fastText
# detector runs on a worker; this cheap writing-system check runs synchronously
# and rejects the common case where a preset rewrites text into another language.
_SCRIPT_TESTS=(
    ('cjk',lambda char:'\u3040'<=char<='\u30ff' or '\u3400'<=char<='\u9fff' or '\uf900'<=char<='\ufaff'),
    ('hangul',lambda char:'\u1100'<=char<='\u11ff' or '\uac00'<=char<='\ud7af'),
    ('cyrillic',lambda char:'\u0400'<=char<='\u04ff'),
    ('arabic',lambda char:'\u0600'<=char<='\u06ff' or '\u0750'<=char<='\u077f'),
    ('hebrew',lambda char:'\u0590'<=char<='\u05ff'),
    ('devanagari',lambda char:'\u0900'<=char<='\u097f'),
    ('thai',lambda char:'\u0e00'<=char<='\u0e7f'),
    ('greek',lambda char:'\u0370'<=char<='\u03ff'),
    ('latin',lambda char:'a'<=char.lower()<='z' or '\u00c0'<=char<='\u024f'),
)


def script_family(text):
    """Dominant writing system, or None for short, empty or mixed text."""
    counts={}
    for char in str(text):
        for name,belongs in _SCRIPT_TESTS:
            if belongs(char):counts[name]=counts.get(name,0)+1;break
    total=sum(counts.values())
    if total<4:return None
    name,count=max(counts.items(),key=lambda item:item[1])
    return name if count>=total*.6 else None


def script_changed(before,after):
    """True only when both texts have a clear, different dominant script."""
    first,second=script_family(before),script_family(after)
    return bool(first and second and first!=second)
