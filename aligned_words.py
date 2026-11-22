"""Validated word maps from Awesome-Align using original Unicode code points."""

MAX_CHARS = 6000


def batches(source, target, spans):
    if max(len(source),len(target)) <= MAX_CHARS:
        return [(0,len(source),0,len(target))]
    result = []
    for s0,s1,t0,t1 in spans:
        if max(s1-s0,t1-t0) > MAX_CHARS:
            return []
        if result and max(s1-result[-1][0],t1-result[-1][2]) <= MAX_CHARS:
            s0,_,t0,_=result.pop()
        result.append((s0,s1,t0,t1))
    return result


def _range(value, limit, origin):
    if (not isinstance(value,list) or len(value)!=2 or any(type(part) is not int for part in value)
            or not 0<=value[0]<value[1]<=limit):
        raise ValueError('Invalid alignment range')
    return value[0]+origin,value[1]+origin


def _read_rows(rows, own, other, start, stop):
    if not isinstance(rows,list) or len(rows)>start[1]-start[0]:
        raise ValueError('Invalid alignment rows')
    result=[]
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get(other),list):
            raise ValueError('Invalid alignment row')
        here=_range(row.get(own),start[1]-start[0],start[0])
        there=tuple(_range(span,stop[1]-stop[0],stop[0]) for span in row[other])
        result.append((here,there))
    return result


def parse(value, bounds):
    if (not isinstance(value,dict) or value.get('protocol_version')!='1'
            or value.get('offset_unit')!='unicode_codepoint'):
        raise ValueError('Unsupported alignment version')
    version=value.get('model_version'),value.get('alignment_version')
    if any(not isinstance(part,str) or not part or len(part)>128 for part in version):
        raise ValueError('Invalid alignment version')
    s0,s1,t0,t1=bounds
    forward=_read_rows(value.get('alignments'),'source','target',(s0,s1),(t0,t1))
    reverse=_read_rows(value.get('reverse_alignments'),'target','source',(t0,t1),(s0,s1))
    if {(own,other) for own,targets in forward for other in targets} != {
            (other,own) for own,sources in reverse for other in sources}:
        raise ValueError('Inconsistent forward and reverse alignment')
    return version,forward,reverse


def corresponding(rows, begin, end):
    matches=sorted({span for (a,b),targets in rows if a<end and b>begin for span in targets})
    result=[]
    for a,b in matches:
        if result and a<=result[-1][1]:
            a,previous_end=result.pop()
            b=max(previous_end,b)
        result.append((a,b))
    return result
