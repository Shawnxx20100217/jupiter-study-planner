import AppKit
import Foundation

guard CommandLine.arguments.count == 3 else {
    fputs("usage: apply_finder_icon <image> <app>\n", stderr)
    exit(64)
}

let imagePath = CommandLine.arguments[1]
let appPath = CommandLine.arguments[2]
guard let image = NSImage(contentsOfFile: imagePath) else {
    fputs("cannot load icon image\n", stderr)
    exit(66)
}
guard NSWorkspace.shared.setIcon(image, forFile: appPath, options: []) else {
    fputs("could not apply Finder custom icon\n", stderr)
    exit(70)
}
