const searchInput = document.getElementById("search-input");
const searchButton = document.getElementById("search-button");

function filterIndex() {
  const query = searchInput.value.trim().toLowerCase();
  document.querySelectorAll(".index-item, .index-commit").forEach(item => {
    item.classList.toggle("hidden", query && !item.textContent.toLowerCase().includes(query));
  });
}

searchInput.addEventListener("input", filterIndex);
searchButton.addEventListener("click", filterIndex);

(function forwardUrlParamsToPageLinks() {
  const search = window.location.search;
  if (!search) return;
  document.querySelectorAll('a[href^="page-"], .pagination a').forEach(anchor => {
    const href = anchor.getAttribute("href") || "";
    if (/^[a-z]+:|^\/\//i.test(href)) return;
    const hashIdx = href.indexOf("#");
    const hash = hashIdx >= 0 ? href.slice(hashIdx) : "";
    const queryIdx = href.indexOf("?");
    const base = href.slice(0, queryIdx >= 0 ? queryIdx : (hashIdx >= 0 ? hashIdx : href.length));
    if (!/^page-\d+\.html/.test(base)) return;
    anchor.setAttribute("href", base + search + hash);
  });
})();
