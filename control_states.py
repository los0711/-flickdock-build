"""Scoped inactive-field ink; never changes geometry or read-only semantics."""


def disabled_fields_qss(scopes, palette, *, fields=('QLineEdit','QComboBox'), internal=(), sliders=False):
    if isinstance(scopes,str):scopes=(scopes,)
    selectors=','.join(f'{scope} {kind}:disabled' for scope in scopes for kind in fields)
    style=(f'\n{selectors} {{background:{palette["background"]};color:{palette["muted"]};'
           f'border-color:{palette["border"]};}}\n') if selectors else ''
    # A spin/shortcut editor shares its parent's backing rather than painting
    # another rectangle inside the already-rounded field.
    editors=','.join(f'{scope} {kind}:disabled QLineEdit' for scope in scopes for kind in internal)
    if editors:style+=f'{editors} {{background:transparent;color:{palette["muted"]};border:0;}}\n'
    if sliders:
        for scope in scopes:
            style+=(f'{scope} QSlider::groove:horizontal:disabled {{background:{palette["control"]};}}\n'
                    f'{scope} QSlider::sub-page:horizontal:disabled {{background:{palette["muted"]};}}\n'
                    f'{scope} QSlider::handle:horizontal:disabled {{background:{palette["muted"]};border-color:{palette["border"]};}}\n')
    return style
