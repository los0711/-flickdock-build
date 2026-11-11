"""Translation targets and local-only clipboard eligibility."""
import re
from functools import cache
from urllib.parse import urlsplit

MAX_TEXT_CHARS = 16000
DETECTION_SAMPLE_CHARS = 1000
DEFAULT_TARGET = 'zh'
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
)
TARGETS = {code: (label, flag) for code, label, flag in LANGUAGES}
SOURCE_NAMES = {code: label for code, label, _ in LANGUAGES}
SOURCE_NAMES.update(zh='中文', no='挪威语', tl='菲律宾语')
SOURCE_NAMES.update({'am':'阿姆哈拉语','az':'阿塞拜疆语','be':'白俄罗斯语','bg':'保加利亚语','bn':'孟加拉语',
    'ca':'加泰罗尼亚语','et':'爱沙尼亚语','eu':'巴斯克语','fa':'波斯语','gu':'古吉拉特语','hr':'克罗地亚语',
    'hu':'匈牙利语','hy':'亚美尼亚语','is':'冰岛语','ka':'格鲁吉亚语','kn':'卡纳达语','ku':'库尔德语',
    'lo':'老挝语','lt':'立陶宛语','lv':'拉脱维亚语','ml':'马拉雅拉姆语','mr':'马拉地语','or':'奥里亚语',
    'pa':'旁遮普语','sk':'斯洛伐克语','sl':'斯洛文尼亚语','sq':'阿尔巴尼亚语','sr':'塞尔维亚语',
    'ta':'泰米尔语','te':'泰卢固语','ur':'乌尔都语','yo':'约鲁巴语'})
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
    import eldc
    eldc.init()
    return eldc


def detect_source(text):
    """ELDC stays lazy and runs on a worker; no copied text leaves the device."""
    eldc = _detector()
    result = eldc.detect_details(text[:DETECTION_SAMPLE_CHARS])
    if not result.reliable or result.language == 'und':
        return '自动识别'
    return SOURCE_NAMES.get(result.language, '自动识别')
