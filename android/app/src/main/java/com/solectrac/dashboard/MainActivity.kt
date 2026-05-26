package com.solectrac.dashboard

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.WebView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.solectrac.dashboard.databinding.ActivityMainBinding
import org.json.JSONObject

/**
 * Hosts the shared dashboard.html (copied from esp32/src/) inside a WebView and
 * pipes BLE-pushed JSON into it via window.dispatchSolectracUpdate.
 */
class MainActivity : AppCompatActivity(), BleClient.Listener {

    private lateinit var b: ActivityMainBinding
    private lateinit var ble: BleClient

    // True once dashboard.html has called SolectracBridge.ready() — only then is
    // it safe to invoke dispatchSolectracUpdate.
    private var bridgeReady = false
    private var pending: String? = null

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { results ->
        if (results.values.all { it }) {
            ble.start()
        } else {
            b.status.text = "Permissions denied"
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)

        configureWebView(b.web)
        ble = BleClient(this, this)

        b.scanBtn.setOnClickListener { ble.rescan() }
    }

    override fun onStart() {
        super.onStart()
        val needed = requiredPermissions()
        val missing = needed.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) ble.start()
        else permissionLauncher.launch(missing.toTypedArray())
    }

    override fun onStop() {
        super.onStop()
        ble.stop()
    }

    private fun requiredPermissions(): List<String> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
        } else {
            listOf(Manifest.permission.ACCESS_FINE_LOCATION)
        }

    private fun configureWebView(web: WebView) {
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.addJavascriptInterface(Bridge(), "SolectracBridge")
        web.loadUrl("file:///android_asset/dashboard.html")
    }

    inner class Bridge {
        /** Called by dashboard.html once it has registered dispatchSolectracUpdate. */
        @JavascriptInterface
        fun ready() {
            runOnUiThread {
                bridgeReady = true
                pending?.let { pushJsonToWebView(it); pending = null }
            }
        }
    }

    private fun pushJsonToWebView(json: String) {
        // JSONObject.quote handles all string escapes safely for JS embedding.
        val escaped = JSONObject.quote(json)
        b.web.evaluateJavascript("window.dispatchSolectracUpdate($escaped);", null)
    }

    // ── BleClient.Listener ────────────────────────────────────────────────────

    override fun onStateChange(state: BleClient.State, detail: String) {
        b.status.text = detail
        val connected = state == BleClient.State.CONNECTED
        // Hide the half-rendered dashboard whenever we're not connected — show a
        // plain "Disconnected" overlay instead.
        b.disconnectedOverlay.visibility = if (connected) View.GONE else View.VISIBLE
        b.disconnectedText.text = when (state) {
            BleClient.State.SCANNING   -> "Scanning…"
            BleClient.State.CONNECTING -> "Connecting…"
            BleClient.State.ERROR      -> "Disconnected"
            else                       -> "Disconnected"
        }
        b.statusBar.visibility = if (connected) View.GONE else View.VISIBLE
        b.scanBtn.visibility =
            if (state == BleClient.State.IDLE || state == BleClient.State.DISCONNECTED ||
                state == BleClient.State.ERROR) View.VISIBLE else View.GONE
    }

    override fun onJson(json: String) {
        if (bridgeReady) pushJsonToWebView(json) else pending = json
    }
}
