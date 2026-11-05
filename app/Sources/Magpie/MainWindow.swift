import AppKit
import SwiftUI
import WebKit

@MainActor
final class MainWindowController: NSWindowController {
    private let model: AppModel

    init(model: AppModel) {
        self.model = model

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1120, height: 760),
            styleMask: [
                .titled,
                .closable,
                .miniaturizable,
                .resizable,
                .fullSizeContentView,
            ],
            backing: .buffered,
            defer: false
        )
        window.title = "Magpie"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = false
        window.minSize = NSSize(width: 900, height: 600)
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("MagpieMainWindow")
        window.center()
        window.contentView = NSHostingView(rootView: MainWindowView(model: model))

        super.init(window: window)
        shouldCascadeWindows = false
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        nil
    }

    func show(route: String? = nil) {
        if let route {
            model.requestRoute(route)
        }
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

struct MainWindowView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        Group {
            if model.backendReady {
                WebView(
                    route: model.webRoute,
                    chatID: model.webChatID,
                    reloadToken: model.webReloadToken
                )
                .ignoresSafeArea()
            } else {
                VStack(spacing: 18) {
                    ProgressView()
                        .controlSize(.large)
                    Text(model.backendMessage)
                        .font(.title3)
                        .foregroundStyle(.secondary)
                    if let error = model.backendError {
                        Text(error)
                            .font(.callout)
                            .foregroundStyle(.red)
                            .multilineTextAlignment(.center)
                            .frame(maxWidth: 540)
                            .textSelection(.enabled)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(nsColor: .windowBackgroundColor))
            }
        }
    }
}

struct WebView: NSViewRepresentable {
    let route: String
    let chatID: Int?
    let reloadToken: Int

    func makeCoordinator() -> Coordinator {
        Coordinator(route: route, chatID: chatID, reloadToken: reloadToken)
    }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.allowsMagnification = true
        webView.load(URLRequest(url: MagpieAPI.webURL(route: route)))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        if context.coordinator.route != route {
            context.coordinator.route = route
            let escaped = route.replacingOccurrences(of: "'", with: "\\'")
            webView.evaluateJavaScript("window.location.hash = '#/\(escaped)';")
        }
        if context.coordinator.chatID != chatID {
            context.coordinator.chatID = chatID
            context.coordinator.appliedChatID = nil
            context.coordinator.openChatIfNeeded(webView)
        }
        if context.coordinator.reloadToken != reloadToken {
            context.coordinator.reloadToken = reloadToken
            webView.reload()
        }
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var route: String
        var chatID: Int?
        var reloadToken: Int
        var appliedChatID: Int?

        init(route: String, chatID: Int?, reloadToken: Int) {
            self.route = route
            self.chatID = chatID
            self.reloadToken = reloadToken
            self.appliedChatID = nil
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            openChatIfNeeded(webView)
        }

        func openChatIfNeeded(_ webView: WKWebView) {
            guard let chatID, appliedChatID != chatID, !webView.isLoading else { return }
            appliedChatID = chatID
            let script = """
            (function () {
                window.location.hash = '#/ask';
                setTimeout(function () {
                    var button = document.getElementById('chatsBtn');
                    if (button) button.click();
                    setTimeout(function () {
                        var item = document.querySelector('[data-chat="\(chatID)"]');
                        if (item) item.click();
                    }, 350);
                }, 400);
            })();
            """
            webView.evaluateJavaScript(script)
        }
    }
}
