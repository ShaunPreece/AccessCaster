"""Windows' `a11y.slider`: `native.slider.SliderCtrl` as a UIA Slider, with the RangeValue pattern
(its numbers) and the Value pattern (its value in words, such as "-20 decibels").

THE SLIDER HAS NO MSAA OF ITS OWN. Windows answers an MSAA client from this provider. JAWS reads a
control that answers MSAA itself through MSAA, and on Up and Down it then speaks the value it held
BEFORE the key, because it speaks before the app has the key; through UIA it speaks the new one.
Left and Right are right either way, and NVDA is right either way.
"""
import ctypes

import wx

from native.win.uia_com import (E_INVALIDARG, OUT_DBL, OUT_INT, OUT_PTR, VARIANT, VT_BSTR,
                                ComObject, WindowProvider, com_method, iid, oleaut32,
                                set_variant, uia)

_VALUE_PATTERN = 10002
_RANGE_VALUE_PATTERN = 10003

_P_CONTROL_TYPE = 30003
_P_NAME = 30005
_P_HAS_FOCUS = 30008
_P_FOCUSABLE = 30009
_P_ENABLED = 30010
_P_HELP_TEXT = 30013
_P_ORIENTATION = 30023
_P_VALUE_VALUE = 30045
_P_RANGE_VALUE = 30047
_P_FULL_DESCRIPTION = 30159

_SLIDER_CONTROL_TYPE = 50015
_ORIENTATION_HORIZONTAL = 1

_IID_RANGE_VALUE = iid("{36DC7AEF-33E6-4691-AFE1-2BE7274B3D33}")
_IID_VALUE = iid("{C7935180-6FB3-4201-B174-7DF73ADBF64A}")

_SET_DBL = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_double)
_SET_STR = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)


class _RangeVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", ctypes.c_void_p), ("AddRef", ctypes.c_void_p),
                ("Release", ctypes.c_void_p),
                ("SetValue", _SET_DBL), ("get_Value", OUT_DBL), ("get_IsReadOnly", OUT_INT),
                ("get_Maximum", OUT_DBL), ("get_Minimum", OUT_DBL),
                ("get_LargeChange", OUT_DBL), ("get_SmallChange", OUT_DBL)]


class _ValueVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", ctypes.c_void_p), ("AddRef", ctypes.c_void_p),
                ("Release", ctypes.c_void_p),
                ("SetValue", _SET_STR), ("get_Value", OUT_PTR), ("get_IsReadOnly", OUT_INT)]


class SliderAccessible(WindowProvider):
    """The slider's UIA element. `words()` is its value as a screen reader says it, and
    `description` is read after it on focus, and can be changed at any time."""

    def __init__(self, window, words, description=""):
        super().__init__(window)
        self.window = window
        self.words = words
        self.description = description
        self._reported = (window.GetValue(), None)

        self._iunknown = self.unknown_methods()       # held: the vtables point into them
        unknown = [ctypes.cast(f, ctypes.c_void_p) for f in self._iunknown]
        self._range_vtbl = _RangeVtbl(*unknown, _SET_DBL(self._range_set),
                                      OUT_DBL(self._range_value), OUT_INT(self._read_only),
                                      OUT_DBL(self._maximum), OUT_DBL(self._minimum),
                                      OUT_DBL(self._large), OUT_DBL(self._small))
        self._value_vtbl = _ValueVtbl(*unknown, _SET_STR(self._value_set),
                                      OUT_PTR(self._value_text), OUT_INT(self._read_only))
        self._range = ComObject(ctypes.addressof(self._range_vtbl))
        self._value = ComObject(ctypes.addressof(self._value_vtbl))
        self._interfaces[_IID_RANGE_VALUE] = ctypes.addressof(self._range)
        self._interfaces[_IID_VALUE] = ctypes.addressof(self._value)
        window.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

    def _on_destroy(self, event):
        if event.GetEventObject() is self.window:
            self.close()
        event.Skip()

    def _pattern(self, pattern_id):
        if pattern_id == _RANGE_VALUE_PATTERN:
            return self._interfaces[_IID_RANGE_VALUE]
        if pattern_id == _VALUE_PATTERN:
            return self._interfaces[_IID_VALUE]
        return None

    def _property(self, property_id):
        w = self.window
        return {
            _P_CONTROL_TYPE: lambda: _SLIDER_CONTROL_TYPE,
            _P_NAME: w.GetLabel,
            _P_HAS_FOCUS: w.HasFocus,
            _P_FOCUSABLE: lambda: True,
            _P_ENABLED: w.IsEnabled,
            _P_ORIENTATION: lambda: _ORIENTATION_HORIZONTAL,
            _P_VALUE_VALUE: self.words,
            _P_RANGE_VALUE: lambda: float(w.GetValue()),
            _P_HELP_TEXT: lambda: self.description or None,
            _P_FULL_DESCRIPTION: lambda: self.description or None,
        }.get(property_id, lambda: None)()

    # IRangeValueProvider
    @com_method
    def _range_set(self, _this, value):
        self.window.set_from_reader(value)

    @com_method
    def _range_value(self, _this, out):
        out[0] = float(self.window.GetValue())

    @com_method
    def _read_only(self, _this, out):
        out[0] = 0

    @com_method
    def _maximum(self, _this, out):
        out[0] = float(self.window.GetMax())

    @com_method
    def _minimum(self, _this, out):
        out[0] = float(self.window.GetMin())

    @com_method
    def _large(self, _this, out):
        out[0] = float(self.window.page_size)

    @com_method
    def _small(self, _this, out):
        out[0] = float(self.window.line_size)

    # IValueProvider
    @com_method
    def _value_set(self, _this, _text):
        return E_INVALIDARG                         # the value is set as a number, by RangeValue

    @com_method
    def _value_text(self, _this, out):
        out[0] = oleaut32.SysAllocString(self.words())

    def changed(self):
        """Tell screen readers the value has changed: both patterns' value-changed events."""
        if self.closed:
            return
        old_value, old_words = self._reported
        new_value, new_words = self.window.GetValue(), self.words()
        self._reported = (new_value, new_words)
        self._raise(_P_RANGE_VALUE, float(old_value), float(new_value))
        self._raise(_P_VALUE_VALUE, old_words, new_words)

    def _raise(self, property_id, old, new):
        before, after = VARIANT(), VARIANT()
        set_variant(before, old)
        set_variant(after, new)
        try:
            uia.UiaRaiseAutomationPropertyChangedEvent(self.ptr, property_id, before, after)
        finally:
            for v in (before, after):
                if v.vt == VT_BSTR:
                    oleaut32.SysFreeString(v.val.ptr)


def slider(window, words, description=""):
    return SliderAccessible(window, words, description)
