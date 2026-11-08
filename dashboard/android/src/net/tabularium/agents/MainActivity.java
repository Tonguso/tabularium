package net.tabularium.agents;
import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.*;
import android.widget.EditText;

public final class MainActivity extends Activity {
    private WebView web;
    private String base;
    private ValueCallback<Uri[]> chooser;
    private PermissionRequest microphone;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        setTitle("Agents");
        base = getPreferences(MODE_PRIVATE).getString("origin", BuildConfig.HOME_URL);
        web = new WebView(this);
        web.setBackgroundColor(0xff050809);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true); // URI explicitly selected in Android's document picker.
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(true);
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                if (OriginPolicy.allows(base, url) && !url.endsWith(".apk")) return false;
                if (request.hasGesture() && "https".equals(request.getUrl().getScheme()))
                    startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl()));
                return true;
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) setTitle("Check Tailscale; use Reload");
            }
            @Override public void onPageFinished(WebView view, String url) { setTitle("Agents"); }
            // Keep the default cancellation of certificate errors; never bypass TLS checks.
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (!OriginPolicy.allows(base, web.getUrl())) return false;
                if (chooser != null) chooser.onReceiveValue(null);
                chooser = callback;
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                intent.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"image/*", "video/*", "audio/*",
                    "application/pdf", "text/plain", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"});
                try { startActivityForResult(intent, 10); }
                catch (Exception error) { chooser.onReceiveValue(null); chooser = null; }
                return true;
            }
            @Override public void onPermissionRequest(PermissionRequest request) {
                if (!OriginPolicy.allows(base, request.getOrigin().toString()) || request.getResources().length != 1 ||
                    !PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(request.getResources()[0])) {
                    request.deny(); return;
                }
                if (microphone != null) microphone.deny();
                microphone = request;
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) grantMicrophone();
                else requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 11);
            }
            @Override public void onPermissionRequestCanceled(PermissionRequest request) {
                if (microphone == request) microphone = null;
            }
        });
        web.setDownloadListener((url, agent, disposition, mime, length) -> {
            if (OriginPolicy.allows(base, url)) startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        });
        setContentView(web);
        web.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                            insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets;
        });
        if (OriginPolicy.origin(base) == null) address(); else web.loadUrl(base + "/#messaging");
    }
    private void grantMicrophone() {
        if (microphone == null) return;
        if (OriginPolicy.allows(base, web.getUrl()) && OriginPolicy.allows(base, microphone.getOrigin().toString()))
            microphone.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
        else microphone.deny();
        microphone = null;
    }
    @Override public void onRequestPermissionsResult(int code, String[] names, int[] results) {
        super.onRequestPermissionsResult(code, names, results);
        if (code == 11 && microphone != null) {
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) grantMicrophone();
            else { microphone.deny(); microphone = null; }
        }
    }
    @Override protected void onActivityResult(int code, int result, Intent data) {
        super.onActivityResult(code, result, data);
        if (code == 10 && chooser != null) {
            Uri uri = result == RESULT_OK && data != null ? data.getData() : null;
            chooser.onReceiveValue(uri != null && "content".equals(uri.getScheme()) ? new Uri[]{uri} : null);
            chooser = null;
        }
    }
    private void address() {
        EditText input = new EditText(this);
        input.setSingleLine(true); input.setInputType(17); input.setText(base);
        input.setHint("https://your-pc.your-tailnet.ts.net");
        new AlertDialog.Builder(this).setTitle("Private dashboard address").setView(input)
            .setMessage("Connect Tailscale first. Only HTTPS Tailscale addresses are accepted.")
            .setNegativeButton("Cancel", null).setPositiveButton("Connect", (dialog, which) -> {
                String value = OriginPolicy.origin(input.getText().toString());
                if (value == null) { address(); return; }
                if (microphone != null) { microphone.deny(); microphone = null; }
                base = value;
                getPreferences(MODE_PRIVATE).edit().putString("origin", base).apply();
                web.loadUrl(base + "/#messaging");
            }).show();
    }
    @Override public boolean onCreateOptionsMenu(Menu menu) { menu.add("Reload"); menu.add("Dashboard address"); return true; }
    @Override public boolean onOptionsItemSelected(MenuItem item) {
        if ("Reload".contentEquals(item.getTitle())) web.loadUrl(base + "/#messaging"); else address();
        return true;
    }
    @Override public void onBackPressed() { if (web.canGoBack()) web.goBack(); else super.onBackPressed(); }
    @Override protected void onPause() {
        web.evaluateJavascript("window.dispatchEvent(new Event('app-background'))", null);
        web.onPause(); super.onPause();
    }
    @Override protected void onResume() { super.onResume(); if (web != null) web.onResume(); }
    @Override protected void onDestroy() {
        if (chooser != null) chooser.onReceiveValue(null);
        if (microphone != null) microphone.deny();
        web.destroy(); super.onDestroy();
    }
}
