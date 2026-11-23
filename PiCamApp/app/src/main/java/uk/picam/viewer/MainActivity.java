package uk.picam.viewer;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
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
import android.widget.TextView;

import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/**
 * PiCam viewer: finds the Pi (saved address, picam.local, or the PiCam hotspot)
 * and shows its stream full screen. The VR and Settings pages come from the Pi.
 */
public class MainActivity extends Activity {

    private static final int PORT = 8000;
    private static final String DEFAULT_ADDRESS = "192.168.0.33";
    private static final String HOTSPOT_ADDRESS = "10.42.0.1";

    private SharedPreferences prefs;
    private WebView web;
    private LinearLayout connectPanel;
    private EditText addressBox;
    private TextView statusText;
    private Button connectButton;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile boolean searching = false;
    private String currentHost = null;

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

        connectPanel = buildConnectPanel();
        root.addView(connectPanel, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        setContentView(root);
        goFullScreen();
        findPi();
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
        if (Build.VERSION.SDK_INT >= 17) s.setMediaPlaybackRequiresUserGesture(false);

        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest req,
                                        WebResourceError err) {
                if (Build.VERSION.SDK_INT >= 21 && req.isForMainFrame()) lostConnection();
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
        String saved = prefs.getString("address", DEFAULT_ADDRESS);
        candidates.add(saved);
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
        // Remember IP addresses that work (not the fallbacks)
        if (!host.equals(HOTSPOT_ADDRESS) && !host.equals("picam.local")) {
            prefs.edit().putString("address", host).apply();
        }
        hideKeyboard();
        connectPanel.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        web.loadUrl(baseUrl(host) + "/");
        goFullScreen();
    }

    // ----------------------------------------------------------- Connect panel
    private LinearLayout buildConnectPanel() {
        LinearLayout p = new LinearLayout(this);
        p.setOrientation(LinearLayout.VERTICAL);
        p.setGravity(Gravity.CENTER);
        p.setBackgroundColor(Color.parseColor("#111111"));
        int pad = dp(28);
        p.setPadding(pad, pad, pad, pad);

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

        TextView tip = new TextView(this);
        tip.setText("Tip: press Back on the camera view to return here.");
        tip.setTextColor(Color.parseColor("#777777"));
        tip.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        tip.setGravity(Gravity.CENTER);
        tip.setPadding(0, dp(20), 0, 0);
        p.addView(tip);
        return p;
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
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.parseColor(colour));
        bg.setCornerRadius(dp(8));
        b.setBackground(bg);
        return b;
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
