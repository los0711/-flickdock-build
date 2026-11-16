"""User accent tokens; semantic transfer/status colors remain independent."""
import re
from functools import lru_cache
from PySide6.QtGui import QColor

DEFAULT='#000000'
_current=DEFAULT

def normalize(value):
    if not isinstance(value,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',value):
        raise ValueError('请选择有效的主题色。')
    return value.upper()

def current():return _current

def set_color(value):
    global _current
    _current=normalize(value)

def _luminance(color):
    values=[channel/255 for channel in (color.red(),color.green(),color.blue())]
    return sum((v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4)*weight
               for v,weight in zip(values,(.2126,.7152,.0722)))

def action_tokens(theme):
    return dict(_action_tokens(_current,theme))

@lru_cache(maxsize=64)
def _action_tokens(value,theme):
    # The neutral black theme retains the existing inverse controls in dark
    # appearance. A custom hue is identical across both appearances.
    if value==DEFAULT:
        return dict(action='#000000' if theme=='light' else '#E5E5E5',
            action_hover='#333333' if theme=='light' else '#D4D4D4',
            action_pressed='#4D4D4D' if theme=='light' else '#C4C4C4',
            on_action='#FFFFFF' if theme=='light' else '#171717')
    color=QColor(value);luminance=_luminance(color)
    on='#FFFFFF' if (1.05/(luminance+.05))>=((luminance+.05)/.05) else '#000000'
    return dict(action=value,on_action=on,
        action_hover=(color.darker(110) if on=='#FFFFFF' else color.lighter(110)).name(),
        action_pressed=(color.darker(120) if on=='#FFFFFF' else color.lighter(120)).name(),ring=value)

def resolve_qss(source,theme):
    for name,value in action_tokens(theme).items():source=source.replace('@fd_'+name+'@',value)
    return source
