// Isolated evidence probe only. Built with the installed Windows SDK; no App SDK.
// Direct/child modes may cover Qt foreground and must pass pixels, not HRESULTs.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <dwmapi.h>
#include <roapi.h>
#include <DispatcherQueue.h>
#include <windows.ui.composition.interop.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Foundation.Numerics.h>
#include <winrt/Windows.System.h>
#include <winrt/Windows.UI.Composition.h>
#include <winrt/Windows.UI.Composition.Desktop.h>
#include <winrt/Windows.UI.ViewManagement.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <memory>
#include <string>
#include <vector>

using namespace winrt;
using namespace winrt::Windows::UI::Composition;
using namespace winrt::Windows::UI::Composition::Desktop;
using namespace winrt::Windows::System;
namespace abi_comp = ABI::Windows::UI::Composition::Desktop;

namespace {
constexpr DWORD host_brush_attribute = DWMWA_USE_HOSTBACKDROPBRUSH;
std::atomic<LONG> contexts{0}, windows{0}, visuals{0};
thread_local std::wstring last_error;
struct ThreadComposition {
    DispatcherQueueController owned_queue{nullptr};
    Compositor compositor{nullptr};
    bool apartment_owned = false;
    ThreadComposition() {
        HRESULT hr = RoInitialize(RO_INIT_SINGLETHREADED);
        if (hr != S_OK && hr != S_FALSE) check_hresult(hr);
        apartment_owned = true;
        if (!DispatcherQueue::GetForCurrentThread()) {
            DispatcherQueueOptions options{sizeof(DispatcherQueueOptions), DQTYPE_THREAD_CURRENT, DQTAT_COM_NONE};
            check_hresult(CreateDispatcherQueueController(options,
                reinterpret_cast<ABI::Windows::System::IDispatcherQueueController**>(put_abi(owned_queue))));
        }
        compositor = Compositor();
    }
    void shutdown() {
        compositor = nullptr;
        if (owned_queue) { owned_queue.ShutdownQueueAsync(); owned_queue = nullptr; }
    }
    ~ThreadComposition() noexcept {
        try { shutdown(); } catch (...) {}
        if (apartment_owned) RoUninitialize();
    }
};
thread_local std::unique_ptr<ThreadComposition> composition;
LRESULT CALLBACK MaterialWndProc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
    if (message == WM_NCHITTEST) return HTTRANSPARENT;
    if (message == WM_MOUSEACTIVATE) return MA_NOACTIVATE;
    if (message == WM_ERASEBKGND) return 1;
    if (message == WM_PAINT) { PAINTSTRUCT ps{}; BeginPaint(hwnd, &ps); EndPaint(hwnd, &ps); return 0; }
    return DefWindowProcW(hwnd, message, wparam, lparam);
}
HWND make_window(HWND host, int mode) {
    HINSTANCE instance = nullptr;
    check_bool(GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(&MaterialWndProc), &instance));
    WNDCLASSEXW wc{sizeof(WNDCLASSEXW)};
    wc.lpfnWndProc = MaterialWndProc; wc.hInstance = instance; wc.lpszClassName = L"FlickDockIsolatedCompositionProbe";
    if (!RegisterClassExW(&wc) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) throw_last_error();
    DWORD ex = WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_NOREDIRECTIONBITMAP;
    DWORD style = mode == 1 ? WS_CHILD : WS_POPUP;
    if (mode >= 2) ex |= WS_EX_TOOLWINDOW;
    HWND result = CreateWindowExW(ex, wc.lpszClassName, L"FlickDock isolated material", style,
        0, 0, 1, 1, mode == 1 ? host : nullptr, nullptr, instance, nullptr);
    check_bool(result != nullptr); ++windows; return result;
}
struct Region { float x, y, width, height, radius, alpha; };
struct OwnedRegion {
    HRGN value = nullptr;
    explicit OwnedRegion(HRGN region) : value(region) { check_bool(region != nullptr); }
    ~OwnedRegion() { if (value) DeleteObject(value); }
};
void set_background_region(HWND background, HWND host, Region const* regions, unsigned count) {
    // This API is intentionally limited to the independently owned backplate.
    // Never crop the Qt foreground/input/shadow host, even in this experiment.
    if (background == host) throw hresult_invalid_argument();
    OwnedRegion total(CreateRectRgn(0, 0, 0, 0));
    for (unsigned index = 0; index < count; ++index) {
        auto const& region = regions[index];
        if (!(region.width > 0 && region.height > 0) || !std::isfinite(region.x) || !std::isfinite(region.y)
            || !std::isfinite(region.width) || !std::isfinite(region.height) || !std::isfinite(region.radius)) throw hresult_invalid_argument();
        if (region.alpha <= 0) continue;
        // HRGNs have binary integer coverage. Do not claim fractional-alpha
        // support or silently turn an animated mask into an opaque one.
        if (region.alpha < 0.999f || !std::isfinite(region.alpha)) throw hresult_error(E_NOTIMPL);
        float radius = std::clamp(region.radius, 0.0f, std::min(region.width, region.height) / 2);
        OwnedRegion part(CreateRoundRectRgn(static_cast<int>(std::lround(region.x)), static_cast<int>(std::lround(region.y)),
            static_cast<int>(std::lround(region.x + region.width)), static_cast<int>(std::lround(region.y + region.height)),
            static_cast<int>(std::lround(radius * 2)), static_cast<int>(std::lround(radius * 2))));
        if (CombineRgn(total.value, total.value, part.value, RGN_OR) == ERROR) throw_last_error();
    }
    // A zero-item mask is an EMPTY HRGN, not nullptr (which removes clipping).
    check_bool(SetWindowRgn(background, total.value, TRUE) != 0);
    total.value = nullptr; // SetWindowRgn succeeded; the system owns this HRGN.
}
struct Element { SpriteVisual visual{nullptr}; CompositionRoundedRectangleGeometry geometry{nullptr}; float alpha = 1; };
struct Context {
    HWND host = nullptr, target_window = nullptr, attribute_window = nullptr;
    DWORD thread_id = 0;
    int mode = 0;
    bool owned_window = false, changed_host_attribute = false, visible = false;
    BOOL prior_host_attribute = FALSE;
    DesktopWindowTarget target{nullptr}; ContainerVisual root{nullptr}; CompositionBackdropBrush brush{nullptr};
    std::vector<Element> elements;
    ~Context() {
        try { if (target) target.Root(nullptr); } catch (...) {}
        visuals -= static_cast<LONG>(elements.size()); elements.clear(); root = nullptr; brush = nullptr; target = nullptr;
        if (changed_host_attribute && IsWindow(attribute_window))
            DwmSetWindowAttribute(attribute_window, host_brush_attribute, &prior_host_attribute, sizeof(prior_host_attribute));
        if (owned_window) { if (IsWindow(target_window)) DestroyWindow(target_window); --windows; }
    }
    void sync_position() {
        RECT rect{}; check_bool(GetClientRect(host, &rect));
        if (owned_window) {
            POINT point{0, 0}; check_bool(ClientToScreen(host, &point));
            HWND after = mode >= 2 ? host : nullptr;
            UINT flags = SWP_NOACTIVATE | SWP_NOOWNERZORDER;
            if (mode == 1) flags |= SWP_NOZORDER;
            check_bool(SetWindowPos(target_window, after, mode == 1 ? 0 : point.x, mode == 1 ? 0 : point.y,
                rect.right - rect.left, rect.bottom - rect.top, flags));
        }
        if (root) root.Size({static_cast<float>(rect.right - rect.left), static_cast<float>(rect.bottom - rect.top)});
    }
};
HRESULT failure() noexcept {
    try { throw; }
    catch (hresult_error const& error) { last_error = error.message().c_str(); return error.code(); }
    catch (std::exception const&) { last_error = L"native C++ exception"; return E_FAIL; }
    catch (...) { last_error = L"unknown native exception"; return E_FAIL; }
}
void valid(Context* context) {
    if (!context || !IsWindow(context->host)) throw hresult_invalid_argument();
    if (GetCurrentThreadId() != context->thread_id) throw hresult_error(RPC_E_WRONG_THREAD);
}
}

// prior_host_flag is known only because the caller creates a fresh dummy Qt
// window and explicitly initializes that attribute. This is NOT a product ABI.
extern "C" __declspec(dllexport) HRESULT __stdcall fm_create(HWND host, int mode, BOOL prior_host_flag, void** output) noexcept {
    if (!output) return E_POINTER; *output = nullptr;
    try {
        DWORD process = 0; GetWindowThreadProcessId(host, &process);
        if (!IsWindow(host) || process != GetCurrentProcessId() || mode < 0 || mode > 3) return E_INVALIDARG;
        if (!composition) composition = std::make_unique<ThreadComposition>();
        auto context = std::make_unique<Context>(); context->host = host; context->mode = mode; context->thread_id = GetCurrentThreadId();
        context->owned_window = mode != 0; context->target_window = mode == 0 ? host : make_window(host, mode);
        if (mode == 3) {
            set_background_region(context->target_window, host, nullptr, 0);
            DWM_SYSTEMBACKDROP_TYPE backdrop = DWMSBT_TRANSIENTWINDOW;
            check_hresult(DwmSetWindowAttribute(context->target_window, DWMWA_SYSTEMBACKDROP_TYPE, &backdrop, sizeof(backdrop)));
            MARGINS margins{-1, -1, -1, -1};
            check_hresult(DwmExtendFrameIntoClientArea(context->target_window, &margins));
            context->sync_position(); *output = context.release(); ++contexts; last_error.clear(); return S_OK;
        }
        context->attribute_window = mode == 1 ? host : context->target_window;
        context->prior_host_attribute = prior_host_flag;
        BOOL enabled = TRUE;
        check_hresult(DwmSetWindowAttribute(context->attribute_window, host_brush_attribute, &enabled, sizeof(enabled)));
        context->changed_host_attribute = true;
        auto interop = composition->compositor.as<abi_comp::ICompositorDesktopInterop>();
        check_hresult(interop->CreateDesktopWindowTarget(context->target_window, FALSE,
            reinterpret_cast<abi_comp::IDesktopWindowTarget**>(put_abi(context->target))));
        context->root = composition->compositor.CreateContainerVisual(); context->target.Root(context->root);
        context->brush = composition->compositor.CreateHostBackdropBrush(); context->sync_position();
        *output = context.release(); ++contexts; last_error.clear(); return S_OK;
    } catch (...) { return failure(); }
}
extern "C" __declspec(dllexport) HRESULT __stdcall fm_update(void* handle, Region const* regions, unsigned count) noexcept {
    try {
        auto* context = static_cast<Context*>(handle); valid(context);
        if (count > 16 || (count && !regions)) return E_INVALIDARG;
        context->sync_position();
        if (context->mode == 3) { set_background_region(context->target_window, context->host, regions, count); return S_OK; }
        while (context->elements.size() > count) {
            context->root.Children().Remove(context->elements.back().visual); context->elements.pop_back(); --visuals;
        }
        while (context->elements.size() < count) {
            Element element; element.visual = composition->compositor.CreateSpriteVisual();
            element.geometry = composition->compositor.CreateRoundedRectangleGeometry();
            element.visual.Clip(composition->compositor.CreateGeometricClip(element.geometry)); element.visual.Brush(context->brush);
            context->root.Children().InsertAtTop(element.visual); context->elements.push_back(element); ++visuals;
        }
        for (unsigned index = 0; index < count; ++index) {
            auto const& region = regions[index]; auto& element = context->elements[index];
            if (!(region.width > 0 && region.height > 0)) return E_INVALIDARG;
            float radius = std::clamp(region.radius, 0.0f, std::min(region.width, region.height) / 2);
            element.geometry.Size({region.width, region.height}); element.geometry.CornerRadius({radius, radius});
            element.visual.Size({region.width, region.height}); element.visual.Offset({region.x, region.y, 0});
            element.alpha = std::clamp(region.alpha, 0.0f, 1.0f); element.visual.Opacity(context->visible ? element.alpha : 0.0f);
        }
        return S_OK;
    } catch (...) { return failure(); }
}
extern "C" __declspec(dllexport) HRESULT __stdcall fm_visible(void* handle, BOOL visible) noexcept {
    try {
        auto* context = static_cast<Context*>(handle); valid(context); context->visible = visible != FALSE;
        for (auto& element : context->elements) element.visual.Opacity(context->visible ? element.alpha : 0.0f);
        if (context->owned_window) ShowWindow(context->target_window, context->visible ? SW_SHOWNOACTIVATE : SW_HIDE);
        context->sync_position();
        return S_OK;
    } catch (...) { return failure(); }
}
extern "C" __declspec(dllexport) HRESULT __stdcall fm_close(void* handle) noexcept {
    try {
        auto* context = static_cast<Context*>(handle);
        if (!context) return E_INVALIDARG;
        if (GetCurrentThreadId() != context->thread_id) return RPC_E_WRONG_THREAD;
        delete context; --contexts; return S_OK;
    } catch (...) { return failure(); }
}
extern "C" __declspec(dllexport) HRESULT __stdcall fm_stats(LONG* output, unsigned size) noexcept {
    if (!output || size < 4) return E_INVALIDARG;
    output[0] = contexts.load(); output[1] = windows.load(); output[2] = visuals.load(); output[3] = composition ? 1 : 0; return S_OK;
}
extern "C" __declspec(dllexport) HRESULT __stdcall fm_error(wchar_t* output, unsigned size) noexcept {
    if (!output || !size) return E_INVALIDARG;
    wcsncpy_s(output, size, last_error.c_str(), _TRUNCATE); return S_OK;
}
extern "C" __declspec(dllexport) HRESULT __stdcall fm_shutdown() noexcept {
    try { if (contexts.load()) return HRESULT_FROM_WIN32(ERROR_BUSY); if (composition) composition->shutdown(); composition.reset(); return S_OK; }
    catch (...) { return failure(); }
}
extern "C" __declspec(dllexport) HRESULT __stdcall fm_policy(LONG* output, unsigned size) noexcept {
    if (!output || size < 2) return E_INVALIDARG;
    HRESULT apartment = RoInitialize(RO_INIT_SINGLETHREADED);
    if (apartment != S_OK && apartment != S_FALSE) return apartment;
    HRESULT result = S_OK;
    try {
        output[0] = winrt::Windows::UI::ViewManagement::UISettings().AdvancedEffectsEnabled() ? 1 : 0;
        output[1] = winrt::Windows::UI::ViewManagement::AccessibilitySettings().HighContrast() ? 1 : 0;
    } catch (...) { result = failure(); }
    RoUninitialize(); return result;
}
