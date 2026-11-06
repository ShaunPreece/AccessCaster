"""Windows' `announce.Notifier`: a UIA notification event, raised on a provider of our own. Heard
in NVDA, JAWS and Narrator.

The provider is served from a window's WM_GETOBJECT. One from UiaHostProviderFromHwnd alone returns
S_OK and is heard by nobody: readers ignore UIA events from a window that has no server-side
provider. Shared with Claudio, which carries the same helper.
"""
import ctypes
from ctypes import wintypes

import diagnostics

HRESULT = ctypes.c_long
LRESULT = ctypes.c_ssize_t
S_OK = 0
E_NOINTERFACE = 0x80004002 - (1 << 32)

WM_GETOBJECT = 0x003D
UIA_ROOT_OBJECT_ID = -25
GWLP_WNDPROC = -4

# ProviderOptions: ServerSideProvider | UseComThreading. COM threading makes UIA call the provider
# on the thread that created it, the UI thread, which is the only place these Python callbacks are
# safe to run.
_PROVIDER_OPTIONS = 0x2 | 0x20

_UIA_NAME_PROPERTY = 30005
_UIA_CONTROL_TYPE_PROPERTY = 30003
_UIA_TEXT_CONTROL_TYPE = 50020

_NOTIFICATION_KIND_OTHER = 4
# NotificationProcessing. ALL queues behind what is being read, and Tab cuts it off in Narrator,
# where IMPORTANT_ALL would not let it. MOST_RECENT is for short lines a newer one should replace.
_ALL = 2
_MOST_RECENT = 3

# Private instances, so these argtypes reach no other module's calls.
_user32 = ctypes.WinDLL("user32")
_oleaut32 = ctypes.WinDLL("oleaut32")
_ole32 = ctypes.WinDLL("ole32")
_uia = ctypes.WinDLL("uiautomationcore")

_oleaut32.SysAllocString.restype = ctypes.c_void_p
_oleaut32.SysAllocString.argtypes = [ctypes.c_wchar_p]
_oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]

_uia.UiaHostProviderFromHwnd.restype = HRESULT
_uia.UiaHostProviderFromHwnd.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_void_p)]
_uia.UiaReturnRawElementProvider.restype = LRESULT
_uia.UiaReturnRawElementProvider.argtypes = [
    wintypes.HWND, wintypes.WPARAM, wintypes.LPARAM, ctypes.c_void_p]
_uia.UiaRaiseNotificationEvent.restype = HRESULT
_uia.UiaRaiseNotificationEvent.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
_uia.UiaDisconnectProvider.restype = HRESULT
_uia.UiaDisconnectProvider.argtypes = [ctypes.c_void_p]

_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)
_user32.SetWindowLongPtrW.restype = ctypes.c_void_p
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
_user32.CallWindowProcW.restype = LRESULT
_user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]


class _Guid(ctypes.Structure):
    _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD),
                ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]


def _iid(text):
    out = _Guid()
    if _ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(out)) != 0:
        raise ValueError(f"bad guid {text}")
    return bytes(out)


_IID_IUNKNOWN = _iid("{00000000-0000-0000-C000-000000000046}")
_IID_RAW_ELEMENT_PROVIDER_SIMPLE = _iid("{D6DD68D1-86FD-4332-8666-9ABEDEA2D24C}")


class _VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("val", ctypes.c_void_p), ("val2", ctypes.c_void_p)]


_VT_I4 = 3
_VT_BSTR = 8

_QI = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(_Guid),
                         ctypes.POINTER(ctypes.c_void_p))
_REF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_OPTIONS = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
_PATTERN = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_int,
                              ctypes.POINTER(ctypes.c_void_p))
_PROPERTY = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_int,
                               ctypes.POINTER(_VARIANT))
_HOST = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))


class _Vtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _QI), ("AddRef", _REF), ("Release", _REF),
                ("get_ProviderOptions", _OPTIONS),
                ("GetPatternProvider", _PATTERN),
                ("GetPropertyValue", _PROPERTY),
                ("get_HostRawElementProvider", _HOST)]


class _ComObject(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_Vtbl))]


class Notifier:
    """An IRawElementProviderSimple served from the window's WM_GETOBJECT, and the notifications
    raised on it.

    The window is subclassed until `close`. The COM object lives as long as this Python object,
    whatever its reference count says: the count is kept honestly for UIA, but the memory is ours.
    """

    def __init__(self, window, name):
        hwnd = window.GetHandle()
        self.hwnd = hwnd
        self.name = name
        self._refs = 1
        self._vtbl = _Vtbl(_QI(self._query), _REF(self._add_ref), _REF(self._release),
                           _OPTIONS(self._options), _PATTERN(self._pattern),
                           _PROPERTY(self._property), _HOST(self._host))
        self._obj = _ComObject(ctypes.pointer(self._vtbl))
        self._ptr = ctypes.addressof(self._obj)
        self._proc = _WNDPROC(self._wndproc)
        self._old_proc = _user32.SetWindowLongPtrW(
            hwnd, GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p))
        self._closed = False

    # IUnknown
    def _query(self, _this, riid, ppv):
        if bytes(riid.contents) in (_IID_IUNKNOWN, _IID_RAW_ELEMENT_PROVIDER_SIMPLE):
            ppv[0] = self._ptr
            self._refs += 1
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _add_ref(self, _this):
        self._refs += 1
        return self._refs

    def _release(self, _this):
        self._refs -= 1
        return self._refs

    # IRawElementProviderSimple
    def _options(self, _this, out):
        out[0] = _PROVIDER_OPTIONS
        return S_OK

    def _pattern(self, _this, _pattern_id, out):
        out[0] = None
        return S_OK

    def _property(self, _this, property_id, out):
        value = out.contents
        value.vt = 0
        if property_id == _UIA_NAME_PROPERTY:
            value.vt = _VT_BSTR
            value.val = _oleaut32.SysAllocString(self.name)
        elif property_id == _UIA_CONTROL_TYPE_PROPERTY:
            value.vt = _VT_I4
            value.val = _UIA_TEXT_CONTROL_TYPE
        return S_OK

    def _host(self, _this, out):
        host = ctypes.c_void_p()
        hr = _uia.UiaHostProviderFromHwnd(self.hwnd, ctypes.byref(host))
        out[0] = host.value
        return hr

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if (msg == WM_GETOBJECT
                and ctypes.c_long(lparam & 0xFFFFFFFF).value == UIA_ROOT_OBJECT_ID):
            # A reader's first WM_GETOBJECT arrives as an input-synchronous call, inside which COM
            # refuses UIA's outgoing call with 0x8001010d (RPC_E_CANTCALLOUT_ININPUTSYNCCALL). UIA
            # catches it and carries on; faulthandler would log it as a crash.
            with diagnostics.handled_native_exceptions():
                return _uia.UiaReturnRawElementProvider(hwnd, wparam, lparam, self._ptr)
        return _user32.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)

    def say(self, text, recent=False):
        if self._closed or not text:
            return None
        display = _oleaut32.SysAllocString(text)
        # The activity id names this app, so a reader that sorts notifications can tell its lines
        # from another program's.
        act = _oleaut32.SysAllocString("accesscaster")
        try:
            return _uia.UiaRaiseNotificationEvent(
                self._ptr, _NOTIFICATION_KIND_OTHER, _MOST_RECENT if recent else _ALL,
                display, act)
        finally:
            _oleaut32.SysFreeString(display)
            _oleaut32.SysFreeString(act)

    def close(self):
        if self._closed:
            return
        self._closed = True
        _uia.UiaReturnRawElementProvider(self.hwnd, 0, 0, None)
        _uia.UiaDisconnectProvider(self._ptr)
        _user32.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self._old_proc)
