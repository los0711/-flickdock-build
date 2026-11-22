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
guard let window = get(app, kAXFocusedWindowAttribute) else {
    FileHandle.standardError.write(Data("Finder did not expose its focused window\n".utf8))
    exit(6)
}
var pending = [window as! AXUIElement]
var inspected = 0
var locations: [(CGPoint, CGSize, String)] = []
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
            let role = get(node, kAXRoleAttribute) as? String ?? ""
            locations.append((point, size, role))
        }
    }
    pending.append(contentsOf: (get(node, kAXChildrenAttribute) as? [AXUIElement]) ?? [])
}
if let (point, size, role) = locations.sorted(by: { a, b in
    let priority: [String: Int] = [kAXStaticTextRole: 0, kAXImageRole: 1, kAXCellRole: 2, kAXRowRole: 4]
    let ar = priority[a.2, default: 3], br = priority[b.2, default: 3]
    if ar != br { return ar < br }
    return a.1.width * a.1.height < b.1.width * b.1.height
}).first {
    // Finder's list-view AXRow can be as wide as the window. The left side
    // holds the visible file instead of the blank space between columns.
    let xOffset = role == kAXRowRole ? min(size.width / 2, 28) : size.width / 2
    print("\(point.x + xOffset) \(point.y + size.height / 2)")
    exit(0)
}
FileHandle.standardError.write(Data("Finder fixture has no accessible bounds (inspected \(inspected) elements)\n".utf8))
exit(5)
