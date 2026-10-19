"use strict";
/* A small project-settings component. Supplied metadata is rendered as text. */
window.SMSProfileUI = {
  render(panel, view, save) {
    panel.replaceChildren();
    if (!view) return;
    const body = document.createElement("div");
    body.className = "pad";
    const heading = document.createElement("h3");
    heading.textContent = "SMS / GG cartridge mapping";
    const status = document.createElement("p");
    status.className = "muted small";
    status.textContent = view.message;
    body.append(heading, status);
    const profile = view.profile || {};
    const select = (label, choices, value) => {
      const field = document.createElement("label");
      field.className = "field";
      const title = document.createElement("span");
      title.textContent = label;
      const input = document.createElement("select");
      for (const [key, name] of choices) {
        const option = document.createElement("option");
        option.value = key;
        option.textContent = name;
        input.append(option);
      }
      input.value = value;
      field.append(title, input);
      body.append(field);
      return input;
    };
    const pending = (name) => (view.supported_mappers || []).includes(name) ? "" : " (analysis pending)";
    const mapper = select("Cartridge mapper", [["unknown", "Unresolved"], ["sega", "Sega"],
      ["mapperless", "No mapper" + pending("mapperless")], ["codemasters", "Codemasters" + pending("codemasters")],
      ["unsupported", "Other unsupported mapper"]], profile.mapper || "unknown");
    const variant = select("Build intent", [["exact-original", "Original reconstruction"],
      ["annotated-original", "Annotated original"], ["experimental", "Experimental / modification research"]],
    profile.variant || "annotated-original");
    const label = document.createElement("label");
    label.className = "field";
    const title = document.createElement("span");
    title.textContent = "Evidence for this choice";
    const reason = document.createElement("textarea");
    reason.value = profile.reason || "";
    reason.minLength = 20;
    reason.maxLength = 1000;
    label.append(title, reason);
    const button = document.createElement("button");
    button.className = "primary small-btn";
    button.type = "button";
    button.textContent = "Save cartridge profile";
    button.onclick = async () => {
      button.disabled = true;
      try { await save({ mapper: mapper.value, variant: variant.value, reason: reason.value }); }
      finally { button.disabled = false; }
    };
    body.append(label, button);
    panel.append(body);
  }
};
