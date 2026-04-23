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
