// Config/Reports mode switcher, shared by templates/index.html and
// templates/owner.html. Mirrors the old config editor's Reports/Config
// switch: Reports mode shows a full-height iframe proxied from GET /reports
// (server.py); Config mode shows this page's own normal content.
//
// The iframe is (re)created on every switch into Reports mode, not created
// once and hidden/shown, so that returning to Reports always reloads the
// report index rather than showing a stale one from earlier in the visit.
(function () {
  var reportsBtn = document.getElementById("mode-btn-reports");
  var configBtn = document.getElementById("mode-btn-config");
  var reportsPane = document.getElementById("reports-pane");
  var configPane = document.getElementById("config-pane");
  var navbar = document.querySelector(".navbar");
  if (!reportsBtn || !configBtn || !reportsPane || !configPane) return;

  function showReports() {
    reportsBtn.classList.add("active");
    configBtn.classList.remove("active");
    configPane.classList.add("d-none");
    reportsPane.classList.remove("d-none");
    // Fills the viewport below the navbar; not expressible in plain CSS
    // without giving the whole page a fixed flex layout.
    reportsPane.style.height = "calc(100vh - " + navbar.offsetHeight + "px)";
    reportsPane.innerHTML = "";
    var iframe = document.createElement("iframe");
    iframe.id = "reports-frame";
    iframe.src = "/reports/";
    reportsPane.appendChild(iframe);
  }

  function showConfig() {
    configBtn.classList.add("active");
    reportsBtn.classList.remove("active");
    reportsPane.classList.add("d-none");
    reportsPane.innerHTML = "";
    configPane.classList.remove("d-none");
  }

  reportsBtn.addEventListener("click", showReports);
  configBtn.addEventListener("click", showConfig);
})();
