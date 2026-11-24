package uk.picam.viewer;

import android.app.Activity;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/**
 * PiCam viewer: finds the Pi (saved address, picam.local, or the PiCam hotspot)
 * and shows its stream full screen. The VR and Settings pages come from the Pi.
 * Press Back to reach the main screen, which also has the Update button.
 */
public class MainActivity extends Activity {

    private static final int PORT = 8000;
    private static final String DEFAULT_ADDRESS = "192.168.0.33";
    private static final String HOTSPOT_ADDRESS = "10.42.0.1";

    // Updates are taken from the latest release in this GitHub repository
    private static final String GITHUB_REPO = "Ashleysmith1988/PiCam";
    private static final String ACTION_INSTALL_STATUS = "uk.picam.viewer.INSTALL_STATUS";

    private SharedPreferences prefs;
    private WebView web;
    private View connectPanel;
    private EditText addressBox;
    private TextView statusText;
    private TextView versionText;
    private Button connectButton;
    private Button updateButton;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile boolean searching = false;
    private volatile boolean updating = false;
    private String currentHost = null;
    private String updateApkUrl = null;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        prefs = getSharedPreferences("picam", MODE_PRIVATE);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        web = new WebView(this);
        setupWebView();
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        // Small "Home" button over the camera view (top left, away from the
        // Pi page's own VR/Settings buttons). The main screen covers it when shown.
        Button home = new Button(this);
        home.setText("⌂ Home");
        home.setAllCaps(false);
        home.setTextColor(Color.WHITE);
        home.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        home.setPadding(dp(14), 0, dp(14), 0);
        styleButton(home, "#88000000");
        home.setOnClickListener(v -> showPanel(currentHost != null
                ? "Connected to " + currentHost + ".\nPress Back to return to the camera."
                : ""));
        FrameLayout.LayoutParams hlp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, dp(44), Gravity.TOP | Gravity.START);
        hlp.setMargins(dp(10), dp(10), 0, 0);
        root.addView(home, hlp);

        connectPanel = buildConnectPanel();
        root.addView(connectPanel, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        setContentView(root);
        goFullScreen();
        findPi();
        checkForUpdate(false);   // quiet check; the button changes if one is found
    }

    // ---------------------------------------------------------------- WebView
    private void setupWebView() {
        web.setBackgroundColor(Color.BLACK);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setMediaPlaybackRequiresUserGesture(false);

        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest req,
                                        WebResourceError err) {
                if (req.isForMainFrame()) lostConnection();
            }

            @SuppressWarnings("deprecation")
            @Override
            public void onReceivedError(WebView view, int code, String desc, String url) {
                if (Build.VERSION.SDK_INT < 23) lostConnection();
            }
        });
    }

    private void lostConnection() {
        ui.post(() -> {
            web.loadUrl("about:blank");
            showPanel("Lost connection to PiCam. Searching again...");
            findPi();
        });
    }

    // ---------------------------------------------------------- Finding the Pi
    private void findPi() {
        if (searching) return;
        searching = true;
        connectButton.setEnabled(false);

        final List<String> candidates = new ArrayList<>();
        candidates.add(prefs.getString("address", DEFAULT_ADDRESS));
        if (!candidates.contains("picam.local")) candidates.add("picam.local");
        if (!candidates.contains(HOTSPOT_ADDRESS)) candidates.add(HOTSPOT_ADDRESS);

        showPanel("Looking for PiCam...");
        new Thread(() -> {
            String found = null;
            for (String host : candidates) {
                final String h = host;
                ui.post(() -> statusText.setText("Trying " + h + "..."));
                if (reachable(host)) { found = host; break; }
            }
            final String result = found;
            ui.post(() -> {
                searching = false;
                connectButton.setEnabled(true);
                if (result != null) {
                    openStream(result);
                } else {
                    showPanel("Couldn't find PiCam.\n\n"
                            + "At home: make sure this tablet is on your home Wi-Fi.\n"
                            + "Out and about: join the PiCam Wi-Fi network.\n\n"
                            + "Or type the Pi's address below.");
                }
            });
        }).start();
    }

    private boolean reachable(String host) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(baseUrl(host) + "/api/status").openConnection();
            c.setConnectTimeout(2500);
            c.setReadTimeout(4000);
            return c.getResponseCode() == 200;
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private static String cleanHost(String input) {
        String h = input.trim();
        h = h.replaceFirst("(?i)^https?://", "");
        int slash = h.indexOf('/');
        if (slash >= 0) h = h.substring(0, slash);
        return h;
    }

    private static String baseUrl(String host) {
        return host.contains(":") ? "http://" + host : "http://" + host + ":" + PORT;
    }

    private void openStream(String host) {
        currentHost = host;
        if (!host.equals(HOTSPOT_ADDRESS) && !host.equals("picam.local")) {
            prefs.edit().putString("address", host).apply();
        }
        hideKeyboard();
        connectPanel.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        web.loadUrl(baseUrl(host) + "/");
        goFullScreen();
    }

    // ----------------------------------------------------------------- Updates
    private int installedBuild() {
        try {
            PackageInfo pi = getPackageManager().getPackageInfo(getPackageName(), 0);
            if (Build.VERSION.SDK_INT >= 28) return (int) pi.getLongVersionCode();
            //noinspection deprecation
            return pi.versionCode;
        } catch (Exception e) {
            return 0;
        }
    }

    private static String readAll(InputStream in) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        in.close();
        return out.toString("UTF-8");
    }

    /** Asks GitHub for the latest release and compares its build number with ours. */
    private void checkForUpdate(final boolean userAsked) {
        if (updating) return;
        if (userAsked) {
            updateButton.setEnabled(false);
            updateButton.setText("Checking for updates...");
        }
        new Thread(() -> {
            int latest = -1;
            String apkUrl = null;
            String error = null;
            HttpURLConnection c = null;
            try {
                c = (HttpURLConnection) new URL("https://api.github.com/repos/"
                        + GITHUB_REPO + "/releases/latest").openConnection();
                c.setRequestProperty("User-Agent", "PiCam-app");
                c.setRequestProperty("Accept", "application/vnd.github+json");
                c.setConnectTimeout(6000);
                c.setReadTimeout(10000);
                int code = c.getResponseCode();
                if (code == 200) {
                    JSONObject j = new JSONObject(readAll(c.getInputStream()));
                    String digits = j.optString("tag_name", "").replaceAll("\\D", "");
                    if (!digits.isEmpty()) latest = Integer.parseInt(digits);
                    JSONArray assets = j.optJSONArray("assets");
                    if (assets != null) {
                        for (int i = 0; i < assets.length(); i++) {
                            JSONObject a = assets.getJSONObject(i);
                            if (a.optString("name").toLowerCase().endsWith(".apk")) {
                                apkUrl = a.optString("browser_download_url");
                                break;
                            }
                        }
                    }
                } else if (code == 404) {
                    error = "no releases published yet";
                } else {
                    error = "GitHub replied " + code;
                }
            } catch (Exception e) {
                error = "no internet connection";
            } finally {
                if (c != null) c.disconnect();
            }

            final int latestBuild = latest;
            final String url = apkUrl;
            final String err = error;
            ui.post(() -> {
                updateButton.setEnabled(true);
                int mine = installedBuild();
                if (url != null && latestBuild > mine) {
                    updateApkUrl = url;
                    updateButton.setText("Install update (build " + latestBuild + ")");
                    styleButton(updateButton, "#2E9E4F");
                } else if (userAsked) {
                    updateButton.setText(err != null
                            ? "Couldn't check: " + err + " - tap to retry"
                            : "Up to date (build " + mine + ") - tap to check again");
                }
            });
        }).start();
    }

    private void onUpdateButton() {
        if (updateApkUrl == null) {
            checkForUpdate(true);
            return;
        }
        // Android 8+: PiCam needs permission to install apps (asked once)
        if (Build.VERSION.SDK_INT >= 26 && !getPackageManager().canRequestPackageInstalls()) {
            updateButton.setText("Allow PiCam to install apps, then tap here again");
            startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + getPackageName())));
            return;
        }
        downloadAndInstall(updateApkUrl);
    }

    private void downloadAndInstall(final String apkUrl) {
        updating = true;
        updateButton.setEnabled(false);
        updateButton.setText("Downloading update...");
        new Thread(() -> {
            HttpURLConnection c = null;
            PackageInstaller.Session session = null;
            try {
                c = (HttpURLConnection) new URL(apkUrl).openConnection();
                c.setInstanceFollowRedirects(true);
                c.setRequestProperty("User-Agent", "PiCam-app");
                c.setConnectTimeout(10000);
                c.setReadTimeout(30000);
                if (c.getResponseCode() != 200)
                    throw new Exception("download failed (" + c.getResponseCode() + ")");
                final long total = c.getContentLength();

                PackageInstaller installer = getPackageManager().getPackageInstaller();
                PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(
                        PackageInstaller.SessionParams.MODE_FULL_INSTALL);
                session = installer.openSession(installer.createSession(params));

                InputStream in = c.getInputStream();
                OutputStream out = session.openWrite("picam.apk", 0, total > 0 ? total : -1);
                byte[] buf = new byte[65536];
                long done = 0;
                int n, lastPct = -1;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                    done += n;
                    if (total > 0) {
                        final int pct = (int) (done * 100 / total);
                        if (pct != lastPct) {
                            lastPct = pct;
                            ui.post(() -> updateButton.setText("Downloading update... " + pct + "%"));
                        }
                    }
                }
                session.fsync(out);
                out.close();
                in.close();

                Intent status = new Intent(this, MainActivity.class)
                        .setAction(ACTION_INSTALL_STATUS);
                int flags = PendingIntent.FLAG_UPDATE_CURRENT;
                if (Build.VERSION.SDK_INT >= 31) flags |= PendingIntent.FLAG_MUTABLE;
                PendingIntent pi = PendingIntent.getActivity(this, 0, status, flags);
                session.commit(pi.getIntentSender());
                session.close();
                session = null;
                ui.post(() -> updateButton.setText("Installing..."));
            } catch (final Exception e) {
                if (session != null) session.abandon();
                ui.post(() -> {
                    updating = false;
                    updateButton.setEnabled(true);
                    updateButton.setText("Update failed: " + e.getMessage() + " - tap to retry");
                });
            } finally {
                if (c != null) c.disconnect();
            }
        }).start();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (!ACTION_INSTALL_STATUS.equals(intent.getAction())) return;
        int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -999);
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            //noinspection deprecation
            Intent confirm = intent.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirm != null) startActivity(confirm);   // Android's "Install?" prompt
        } else if (status == PackageInstaller.STATUS_SUCCESS) {
            updateButton.setText("Updated - reopen PiCam");
        } else {
            updating = false;
            updateButton.setEnabled(true);
            String msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
            updateButton.setText("Update cancelled or failed"
                    + (msg != null ? ": " + msg : "") + " - tap to retry");
        }
    }

    // ----------------------------------------------------------- Main screen
    private View buildConnectPanel() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(Color.parseColor("#111111"));

        LinearLayout p = new LinearLayout(this);
        p.setOrientation(LinearLayout.VERTICAL);
        p.setGravity(Gravity.CENTER);
        int pad = dp(28);
        p.setPadding(pad, pad, pad, pad);
        scroll.addView(p, new ScrollView.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        TextView title = new TextView(this);
        title.setText("PiCam");
        title.setTextColor(Color.WHITE);
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 32);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setGravity(Gravity.CENTER);
        p.addView(title);

        statusText = new TextView(this);
        statusText.setTextColor(Color.parseColor("#BBBBBB"));
        statusText.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        statusText.setGravity(Gravity.CENTER);
        statusText.setPadding(0, dp(16), 0, dp(20));
        p.addView(statusText, new LinearLayout.LayoutParams(dp(420),
                ViewGroup.LayoutParams.WRAP_CONTENT));

        addressBox = new EditText(this);
        addressBox.setText(prefs.getString("address", DEFAULT_ADDRESS));
        addressBox.setHint("Pi address, e.g. 192.168.0.33");
        addressBox.setTextColor(Color.WHITE);
        addressBox.setHintTextColor(Color.GRAY);
        addressBox.setSingleLine(true);
        addressBox.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        addressBox.setImeOptions(EditorInfo.IME_ACTION_GO);
        addressBox.setOnEditorActionListener((v, id, ev) -> { connectTyped(); return true; });
        p.addView(addressBox, new LinearLayout.LayoutParams(dp(420),
                ViewGroup.LayoutParams.WRAP_CONTENT));

        connectButton = makeButton("Connect", "#2B6CDF");
        connectButton.setOnClickListener(v -> connectTyped());
        p.addView(connectButton, buttonParams());

        Button hotspot = makeButton("Use PiCam hotspot (10.42.0.1)", "#333333");
        hotspot.setOnClickListener(v -> {
            addressBox.setText(HOTSPOT_ADDRESS);
            connectTyped();
        });
        p.addView(hotspot, buttonParams());

        Button retry = makeButton("Search again", "#333333");
        retry.setOnClickListener(v -> findPi());
        p.addView(retry, buttonParams());

        updateButton = makeButton("Check for updates", "#333333");
        updateButton.setOnClickListener(v -> onUpdateButton());
        LinearLayout.LayoutParams ulp = buttonParams();
        ulp.topMargin = dp(28);
        p.addView(updateButton, ulp);

        versionText = new TextView(this);
        versionText.setText("PiCam app build " + installedBuild());
        versionText.setTextColor(Color.parseColor("#777777"));
        versionText.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        versionText.setGravity(Gravity.CENTER);
        versionText.setPadding(0, dp(10), 0, 0);
        p.addView(versionText);

        TextView tip = new TextView(this);
        tip.setText("Tip: press Back on the camera view to return here.");
        tip.setTextColor(Color.parseColor("#777777"));
        tip.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        tip.setGravity(Gravity.CENTER);
        tip.setPadding(0, dp(6), 0, 0);
        p.addView(tip);
        return scroll;
    }

    private void connectTyped() {
        final String host = cleanHost(addressBox.getText().toString());
        if (host.isEmpty() || searching) return;
        hideKeyboard();
        searching = true;
        connectButton.setEnabled(false);
        statusText.setText("Connecting to " + host + "...");
        new Thread(() -> {
            final boolean ok = reachable(host);
            ui.post(() -> {
                searching = false;
                connectButton.setEnabled(true);
                if (ok) {
                    prefs.edit().putString("address", host).apply();
                    openStream(host);
                } else {
                    statusText.setText("No answer from " + host + ".\n"
                            + "Check the address and that the tablet is on the same Wi-Fi as the Pi.");
                }
            });
        }).start();
    }

    private void showPanel(String message) {
        statusText.setText(message);
        connectPanel.setVisibility(View.VISIBLE);
    }

    private Button makeButton(String text, String colour) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextColor(Color.WHITE);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        styleButton(b, colour);
        return b;
    }

    private void styleButton(Button b, String colour) {
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.parseColor(colour));
        bg.setCornerRadius(dp(8));
        b.setBackground(bg);
    }

    private LinearLayout.LayoutParams buttonParams() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(dp(420), dp(52));
        lp.topMargin = dp(12);
        return lp;
    }

    // ------------------------------------------------------------ Navigation
    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (connectPanel.getVisibility() == View.VISIBLE) {
                if (currentHost != null && web.getUrl() != null
                        && !web.getUrl().equals("about:blank")) {
                    connectPanel.setVisibility(View.GONE);   // back to the stream
                    goFullScreen();
                    return true;
                }
                return super.onKeyDown(keyCode, event);      // exit app
            }
            if (web.canGoBack()) { web.goBack(); return true; }
            showPanel(currentHost != null ? "Connected to " + currentHost
                    + ".\nPress Back again to return to the camera." : "");
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) goFullScreen();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }

    // ---------------------------------------------------------------- Helpers
    @SuppressWarnings("deprecation")
    private void goFullScreen() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
    }

    private void hideKeyboard() {
        InputMethodManager imm = (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
        if (imm != null && addressBox != null)
            imm.hideSoftInputFromWindow(addressBox.getWindowToken(), 0);
    }

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
                getResources().getDisplayMetrics());
    }
}
