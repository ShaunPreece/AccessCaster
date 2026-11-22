"""A UIA provider of the app's own, served from a window's WM_GETOBJECT: the plumbing that
`uia_notify` and `uia_slider` share.

`WindowProvider` is an IRawElementProviderSimple built from ctypes, with no type library. A subclass
answers `_property` and `_pattern`; anything it leaves empty is taken from the window's host
provider (position, visibility, parent). The window is subclassed until `close`, and the COM object
lives as long as the Python object, whatever its reference count says: the count is kept honestly
for UIA, but the memory is ours.
"""
import ctypes
from ctypes import wintypes

import diagnostics

HRESULT = ctypes.c_long
LRESULT = ctypes.c_ssize_t
S_OK = 0
E_NOINTERFACE = 0x80004002 - (1 << 32)
E_FAIL = 0x80004005 - (1 << 32)
E_INVALIDARG = 0x80070057 - (1 << 32)

WM_GETOBJECT = 0x003D
UIA_ROOT_OBJECT_ID = -25
GWLP_WNDPROC = -4

# ProviderOptions: ServerSideProvider | UseComThreading. COM threading makes UIA call the provider
# on the thread that created it, the UI thread, which is the only place these Python callbacks are
# safe to run.
PROVIDER_OPTIONS = 0x2 | 0x20

VT_EMPTY, VT_I4, VT_R8, VT_BSTR, VT_BOOL = 0, 3, 5, 8, 11

user32 = ctypes.WinDLL("user32")
oleaut32 = ctypes.WinDLL("oleaut32")
ole32 = ctypes.WinDLL("ole32")
uia = ctypes.WinDLL("uiautomationcore")

oleaut32.SysAllocString.restype = ctypes.c_void_p
oleaut32.SysAllocString.argtypes = [ctypes.c_wchar_p]
oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]


class _Val(ctypes.Union):
    _fields_ = [("dbl", ctypes.c_double), ("i4", ctypes.c_long), ("b", ctypes.c_short),
                ("ptr", ctypes.c_void_p)]


class VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("val", _Val), ("pad", ctypes.c_void_p)]


def set_variant(v, value):
    """Fill a VARIANT from a Python bool, int, float or str. A str becomes a BSTR the receiver
    frees."""
    if isinstance(value, bool):
        v.vt, v.val.b = VT_BOOL, -1 if value else 0
    elif isinstance(value, int):
        v.vt, v.val.i4 = VT_I4, value
    elif isinstance(value, float):
        v.vt, v.val.dbl = VT_R8, value
    elif isinstance(value, str):
        v.vt, v.val.ptr = VT_BSTR, oleaut32.SysAllocString(value)
    else:
        v.vt = VT_EMPTY


uia.UiaHostProviderFromHwnd.restype = HRESULT
uia.UiaHostProviderFromHwnd.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_void_p)]
uia.UiaReturnRawElementProvider.restype = LRESULT
uia.UiaReturnRawElementProvider.argtypes = [
    wintypes.HWND, wintypes.WPARAM, wintypes.LPARAM, ctypes.c_void_p]
uia.UiaRaiseNotificationEvent.restype = HRESULT
uia.UiaRaiseNotificationEvent.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
uia.UiaRaiseAutomationPropertyChangedEvent.restype = HRESULT
uia.UiaRaiseAutomationPropertyChangedEvent.argtypes = [
    ctypes.c_void_p, ctypes.c_int, VARIANT, VARIANT]
uia.UiaDisconnectProvider.restype = HRESULT
uia.UiaDisconnectProvider.argtypes = [ctypes.c_void_p]

_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
user32.CallWindowProcW.restype = LRESULT
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]


class Guid(ctypes.Structure):
    _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD),
                ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]


def iid(text):
    out = Guid()
    if ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(out)) != 0:
        raise ValueError(f"bad guid {text}")
    return bytes(out)


IID_IUNKNOWN = iid("{00000000-0000-0000-C000-000000000046}")
IID_RAW_ELEMENT_PROVIDER_SIMPLE = iid("{D6DD68D1-86FD-4332-8666-9ABEDEA2D24C}")

QI = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(Guid),
                        ctypes.POINTER(ctypes.c_void_p))
REF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
OUT_INT = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
OUT_PTR = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
OUT_DBL = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_double))
_PATTERN = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_int,
                              ctypes.POINTER(ctypes.c_void_p))
_PROPERTY = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(VARIANT))


class _SimpleVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", QI), ("AddRef", REF), ("Release", REF),
                ("get_ProviderOptions", OUT_INT), ("GetPatternProvider", _PATTERN),
                ("GetPropertyValue", _PROPERTY), ("get_HostRawElementProvider", OUT_PTR)]


class ComObject(ctypes.Structure):
    """A COM object's memory: a pointer to its vtable."""
    _fields_ = [("lpVtbl", ctypes.c_void_p)]


def com_method(fn):
    """A COM method body that never lets an exception reach native code. Returning None is S_OK."""
    def wrapped(*args):
        try:
            result = fn(*args)
            return S_OK if result is None else result
        except Exception as exc:  # noqa: BLE001
            diagnostics.event("uia.error", f"{fn.__qualname__}: {exc!r}")
            return E_FAIL
    return wrapped


class WindowProvider:
    """The window's UIA element. Subclasses override `_property` and `_pattern`, and add
    interfaces of their own through `_interfaces`, a dict of IID to object pointer."""

    def __init__(self, window):
        self.hwnd = window.GetHandle()
        self._refs = 1
        self._closed = False
        self._vtbl = _SimpleVtbl(QI(self._query), REF(self._add_ref), REF(self._release),
                                 OUT_INT(self._options), _PATTERN(self._get_pattern),
                                 _PROPERTY(self._get_property), OUT_PTR(self._host))
        self._obj = ComObject(ctypes.addressof(self._vtbl))
        self.ptr = ctypes.addressof(self._obj)
        self._interfaces = {IID_RAW_ELEMENT_PROVIDER_SIMPLE: self.ptr}
        self._proc = _WNDPROC(self._wndproc)
        self._old_proc = user32.SetWindowLongPtrW(
            self.hwnd, GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p))

    def unknown_methods(self):
        """IUnknown's three entries, for the vtable of another interface on this element."""
        return QI(self._query), REF(self._add_ref), REF(self._release)

    # IUnknown, shared by every interface the element has: each pointer answers for all of them.
    @com_method
    def _query(self, this, riid, ppv):
        iid_bytes = bytes(riid.contents)
        target = this if iid_bytes == IID_IUNKNOWN else self._interfaces.get(iid_bytes)
        if target is None:
            ppv[0] = None
            return E_NOINTERFACE
        ppv[0] = target
        self._refs += 1

    def _add_ref(self, _this):
        self._refs += 1
        return self._refs

    def _release(self, _this):
        self._refs -= 1
        return self._refs

    # IRawElementProviderSimple
    @com_method
    def _options(self, _this, out):
        out[0] = PROVIDER_OPTIONS

    @com_method
    def _get_pattern(self, _this, pattern_id, out):
        out[0] = self._pattern(pattern_id)
        if out[0]:
            self._refs += 1

    @com_method
    def _get_property(self, _this, property_id, out):
        set_variant(out.contents, self._property(property_id))

    @com_method
    def _host(self, _this, out):
        host = ctypes.c_void_p()
        hr = uia.UiaHostProviderFromHwnd(self.hwnd, ctypes.byref(host))
        out[0] = host.value
        return hr

    def _pattern(self, _pattern_id):
        """The pointer to the pattern's interface, or None for a pattern the element lacks."""
        return None

    def _property(self, _property_id):
        """A property's value as bool, int, float or str; None leaves it to the host provider."""
        return None

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if (msg == WM_GETOBJECT
                and ctypes.c_long(lparam & 0xFFFFFFFF).value == UIA_ROOT_OBJECT_ID):
            # A reader's first WM_GETOBJECT arrives as an input-synchronous call, inside which COM
            # refuses UIA's outgoing call with 0x8001010d (RPC_E_CANTCALLOUT_ININPUTSYNCCALL). UIA
            # catches it and carries on; faulthandler would log it as a crash.
            with diagnostics.handled_native_exceptions():
                return uia.UiaReturnRawElementProvider(hwnd, wparam, lparam, self.ptr)
        return user32.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)

    @property
    def closed(self):
        return self._closed

    def close(self):
        if self._closed:
            return
        self._closed = True
        uia.UiaReturnRawElementProvider(self.hwnd, 0, 0, None)
        uia.UiaDisconnectProvider(self.ptr)
        user32.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self._old_proc)
