import AppKit
import ApplicationServices

func get(_ element: AXUIElement, _ key: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, key as CFString, &value) == .success else { return nil }
    return value
}

guard CommandLine.arguments.count == 2 else { exit(2) }
guard AXIsProcessTrusted() else {
    FileHandle.standardError.write(Data("Finder accessibility permission unavailable\n".utf8))
    exit(3)
}
let fixture = CommandLine.arguments[1]
guard let finder = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.finder").first else {
    exit(4)
}
let app = AXUIElementCreateApplication(finder.processIdentifier)
var pending = (get(app, kAXWindowsAttribute) as? [AXUIElement]) ?? []
var inspected = 0
while let node = pending.popLast(), inspected < 5000 {
    inspected += 1
    let labels = [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute, "AXFilename"]
    if labels.contains(where: { get(node, $0) as? String == fixture }),
       let origin = get(node, kAXPositionAttribute), CFGetTypeID(origin) == AXValueGetTypeID(),
       let dimensions = get(node, kAXSizeAttribute), CFGetTypeID(dimensions) == AXValueGetTypeID() {
        var point = CGPoint.zero
        var size = CGSize.zero
        if AXValueGetValue(origin as! AXValue, .cgPoint, &point),
           AXValueGetValue(dimensions as! AXValue, .cgSize, &size), size.width > 0, size.height > 0 {
            print("\(point.x + size.width / 2) \(point.y + size.height / 2)")
            exit(0)
        }
    }
    pending.append(contentsOf: (get(node, kAXChildrenAttribute) as? [AXUIElement]) ?? [])
}
FileHandle.standardError.write(Data("Finder fixture has no accessible bounds (inspected \(inspected) elements)\n".utf8))
exit(5)
