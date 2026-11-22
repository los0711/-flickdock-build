// Desktop Duplication returns the pixels composed for the display and QPC
// timestamps for desktop updates. Only a small rectangle is copied to RAM.
#include <d3d11.h>
#include <dxgi1_2.h>
#include <wrl/client.h>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

using Microsoft::WRL::ComPtr;

struct CaptureSlot {
    ComPtr<ID3D11Texture2D> texture;
    DXGI_OUTDUPL_FRAME_INFO frame{};
};

static HRESULT emit(ID3D11DeviceContext* context, const CaptureSlot& slot, unsigned width,
                    unsigned height, double clockFrequency, bool wait) {
    D3D11_MAPPED_SUBRESOURCE map{};
    const HRESULT result = context->Map(slot.texture.Get(), 0, D3D11_MAP_READ,
                                        wait ? 0 : D3D11_MAP_FLAG_DO_NOT_WAIT, &map);
    if (FAILED(result)) return result;
    const auto* pixels = static_cast<const uint8_t*>(map.pData);
    uint64_t hash = 1469598103934665603ULL;
    for (unsigned row = 0; row < height; ++row) {
        const auto* line = pixels + row * map.RowPitch;
        for (unsigned column = 0; column < width * 4; ++column) {
            hash ^= line[column];
            hash *= 1099511628211ULL;
        }
    }
    context->Unmap(slot.texture.Get(), 0);
    std::printf("%.9f %016llx %u\n", double(slot.frame.LastPresentTime.QuadPart) / clockFrequency,
                static_cast<unsigned long long>(hash), slot.frame.AccumulatedFrames);
    std::fflush(stdout);
    return S_OK;
}

int main(int argc, char** argv) {
    if (argc != 6) return 2;
    const unsigned x = std::strtoul(argv[1], nullptr, 10);
    const unsigned y = std::strtoul(argv[2], nullptr, 10);
    const unsigned width = std::strtoul(argv[3], nullptr, 10);
    const unsigned height = std::strtoul(argv[4], nullptr, 10);
    const double seconds = std::atof(argv[5]);
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0,
                                   D3D11_SDK_VERSION, &device, nullptr, &context);
    if (FAILED(hr)) {
        std::fprintf(stderr, "D3D11CreateDevice: 0x%08lx\n", hr);
        return 3;
    }
    ComPtr<IDXGIDevice> dxgi;
    ComPtr<IDXGIAdapter> adapter;
    ComPtr<IDXGIOutput> output;
    ComPtr<IDXGIOutput1> output1;
    ComPtr<IDXGIOutputDuplication> duplication;
    if (FAILED(hr = device.As(&dxgi)) || FAILED(hr = dxgi->GetAdapter(&adapter))
        || FAILED(hr = adapter->EnumOutputs(0, &output)) || FAILED(hr = output.As(&output1))
        || FAILED(hr = output1->DuplicateOutput(device.Get(), &duplication))) {
        std::fprintf(stderr, "DuplicateOutput: 0x%08lx\n", hr);
        return 4;
    }
    DXGI_OUTDUPL_DESC output_desc{};
    duplication->GetDesc(&output_desc);
    if (!width || !height || x + width > output_desc.ModeDesc.Width || y + height > output_desc.ModeDesc.Height) return 2;
    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = width;
    desc.Height = height;
    desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1;
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    desc.Usage = D3D11_USAGE_STAGING;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    std::array<CaptureSlot, 4> pending;
    for (CaptureSlot& slot : pending)
        if (FAILED(hr = device->CreateTexture2D(&desc, nullptr, &slot.texture))) return 5;
    size_t readIndex = 0, writeIndex = 0;

    LARGE_INTEGER frequency{}, start{}, now{};
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&start);
    const D3D11_BOX crop{x, y, 0, x + width, y + height, 1};
    std::puts("READY");
    std::fflush(stdout);
    do {
        DXGI_OUTDUPL_FRAME_INFO frame{};
        ComPtr<IDXGIResource> surface;
        hr = duplication->AcquireNextFrame(60, &frame, &surface);
        if (SUCCEEDED(hr)) {
            if (frame.LastPresentTime.QuadPart) {
                if (writeIndex - readIndex == pending.size()) {
                    if (FAILED(emit(context.Get(), pending[readIndex % pending.size()], width, height,
                                    static_cast<double>(frequency.QuadPart), true))) return 7;
                    ++readIndex;
                }
                ComPtr<ID3D11Texture2D> texture;
                if (FAILED(hr = surface.As(&texture))) return 6;
                CaptureSlot& slot = pending[writeIndex % pending.size()];
                slot.frame = frame;
                context->CopySubresourceRegion(slot.texture.Get(), 0, 0, 0, 0, texture.Get(), 0, &crop);
                context->Flush();
                ++writeIndex;
            }
            duplication->ReleaseFrame();
        } else if (hr != DXGI_ERROR_WAIT_TIMEOUT) {
            std::fprintf(stderr, "AcquireNextFrame: 0x%08lx\n", hr);
            return 8;
        }
        while (readIndex < writeIndex) {
            hr = emit(context.Get(), pending[readIndex % pending.size()], width, height,
                      static_cast<double>(frequency.QuadPart), false);
            if (hr == DXGI_ERROR_WAS_STILL_DRAWING) break;
            if (FAILED(hr)) return 9;
            ++readIndex;
        }
        QueryPerformanceCounter(&now);
    } while (double(now.QuadPart - start.QuadPart) / frequency.QuadPart < seconds);
    while (readIndex < writeIndex) {
        if (FAILED(emit(context.Get(), pending[readIndex % pending.size()], width, height,
                        static_cast<double>(frequency.QuadPart), true))) return 10;
        ++readIndex;
    }
    return 0;
}
