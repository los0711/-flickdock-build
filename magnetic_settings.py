"""Independent, additive magnetic trigger preferences; no IO or UI ownership."""
from copy import deepcopy

EDGES=('left','right','top','bottom')
LABELS={'left':'左侧','right':'右侧','top':'顶部','bottom':'底部'}

def default_edges():
    return {edge:dict(enabled=edge in ('left','right'),width=80,length=50,position=50) for edge in EDGES}

def defaults():return dict(enabled=True,displays={})

def validate(value):
    if not isinstance(value,dict) or set(value)-{'enabled','displays'}:
        raise ValueError('磁吸设置无效。')
    enabled=value.get('enabled',True);displays=value.get('displays',{})
    if type(enabled) is not bool or not isinstance(displays,dict) or len(displays)>64:
        raise ValueError('磁吸显示器设置无效。')
    result=dict(enabled=enabled,displays={})
    for key,edges in displays.items():
        if not isinstance(key,str) or len(key)!=24 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('磁吸显示器标识无效。')
        if not isinstance(edges,dict) or set(edges)-set(EDGES):raise ValueError('磁吸方向无效。')
        clean=default_edges()
        for edge,row in edges.items():
            if not isinstance(row,dict) or set(row)-{'enabled','width','length','position'}:raise ValueError('磁吸区域无效。')
            candidate={**clean[edge],**row}
            if type(candidate['enabled']) is not bool:raise ValueError('请选择磁吸边缘开关。')
            for name,low,high in (('width',8,240),('length',5,100),('position',0,100)):
                if type(candidate[name]) is not int or not low<=candidate[name]<=high:
                    raise ValueError('磁吸区域参数超出范围。')
            clean[edge]=candidate
        result['displays'][key]=clean
    return result

def for_display(value,key):
    return deepcopy(value['displays'].get(key,default_edges())) if value['enabled'] else {}

def region_rect(area,edge,row):
    """A DIP input rectangle. Visual glow geometry is deliberately independent."""
    from PySide6.QtCore import QRect
    side=edge in ('left','right')
    low=area.top() if side else area.left()
    total=area.height() if side else area.width()
    span=max(1,min(total,round(total*row['length']/100)))
    start=round(low+(total-1)*row['position']/100-span/2)
    start=max(low,min(low+total-span,start))
    depth=min(row['width'],area.width() if side else area.height())
    if edge=='left':return QRect(area.left(),start,depth,span)
    if edge=='right':return QRect(area.right()-depth+1,start,depth,span)
    if edge=='top':return QRect(start,area.top(),span,depth)
    return QRect(start,area.bottom()-depth+1,span,depth)
