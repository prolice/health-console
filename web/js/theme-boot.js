// Runs synchronously in <head>, before the stylesheet paints, so a reader
// who chose the dark theme never sees a white flash first. app.js owns the
// theme from then on; this only sets the initial attribute. Kept as a
// classic script, not a module: modules are deferred by definition, which
// is exactly the delay this exists to avoid.
(function () {
  try {
    var choice = localStorage.getItem("theme") || "auto";
    var dark = choice === "dark" || (choice === "auto"
      && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.setAttribute(
      "data-bs-theme", dark ? "dark" : "light");
  } catch (error) {
    // A browser refusing localStorage (private mode, blocked site data)
    // must not stop the page loading: the light default already stands.
  }
})();
