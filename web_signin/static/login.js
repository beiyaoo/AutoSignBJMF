const form = document.getElementById("loginForm");
const errorText = document.getElementById("loginError");
const loginButton = document.getElementById("btnLogin");
form.addEventListener("submit", async event => {
  event.preventDefault();
  loginButton.disabled = true;
  errorText.textContent = "";
  try {
    const response = await fetch("/api/admin/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: document.getElementById("adminUsername").value.trim(),
                             password: document.getElementById("adminPassword").value })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "登录失败");
    sessionStorage.removeItem("bjmf_admin_token");
    document.getElementById("adminPassword").value = "";
    const target = new URLSearchParams(location.search).get("next");
    location.assign(["/", "/admin", "/manage"].includes(target) ? target : "/");
  } catch (error) { errorText.textContent = error.message; }
  finally { loginButton.disabled = false; }
});
