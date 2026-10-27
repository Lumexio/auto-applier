"""Static HTML application forms for testing question extraction and filling in a real browser."""

# A Rails/Greenhouse-style single-page application form.
GREENHOUSE_LIKE = """<!doctype html><html><body>
<form id="application_form">
  <div class="field"><label for="first_name">First Name <span class="req">*</span></label><input id="first_name" name="first_name" type="text" required></div>
  <div class="field"><label for="email">Email *</label><input id="email" name="email" type="email" required></div>
  <div class="field"><label for="resume">Resume/CV</label><input id="resume" type="file"></div>
  <div class="field"><label for="q_linkedin">LinkedIn Profile</label><input id="q_linkedin" type="text"></div>
  <div class="field"><label for="q_auth">Are you legally authorized to work in the United States? *</label>
    <select id="q_auth" name="q_auth" required><option value="">Select...</option><option value="1">Yes</option><option value="0">No</option></select></div>
  <div class="field"><label for="q_spons">Will you now or in the future require sponsorship for employment visa status? *</label>
    <select id="q_spons" required><option value="">Please select</option><option>Yes</option><option>No</option></select></div>
  <div class="field"><label for="q_py">How many years of experience do you have with Python? *</label><input id="q_py" type="number" required></div>
  <div class="field"><label for="q_why">Why do you want to work at Acme? *</label><textarea id="q_why" maxlength="2000" required></textarea></div>
  <div class="field"><label for="q_gender">Gender</label>
    <select id="q_gender"><option value="">Select</option><option>Male</option><option>Female</option><option>Decline To Self Identify</option></select></div>
  <div class="field"><label><input type="checkbox" id="q_priv" required> I have read and agree to the privacy policy *</label></div>
  <div class="field"><label><input type="checkbox" id="q_news"> Subscribe to our newsletter</label></div>
  <input type="hidden" name="authenticity_token" value="x">
  <div style="display:none"><label for="trap">Hidden trap</label><input id="trap"></div>
  <button type="submit">Submit Application</button>
</form></body></html>"""

# A modal dialog (LinkedIn Easy Apply style) over a page that has its own form, which must be ignored.
MODAL_LIKE = """<!doctype html><html><body>
<div id="bg"><form><label for="bg1">Background field</label><input id="bg1"></form></div>
<div role="dialog" aria-modal="true">
  <h2>Additional Questions</h2>
  <div class="q"><label for="rm"><span aria-hidden="true">Years of work experience with Node.js?</span><span class="visually-hidden">Years of work experience with Node.js?</span></label>
    <input id="rm" type="text"></div>
  <fieldset><legend><span>Are you comfortable working remotely?</span><span class="visually-hidden">Are you comfortable working remotely?</span></legend>
    <div><input type="radio" id="r1" name="remote" value="Yes"><label for="r1">Yes</label></div>
    <div><input type="radio" id="r2" name="remote" value="No"><label for="r2">No</label></div></fieldset>
  <label for="lang">English proficiency <select id="lang"><option value="">Select an option</option><option>Native or bilingual</option><option>Professional</option><option>Conversational</option></select></label>
  <fieldset><legend>Which of these do you know?</legend>
    <label><input type="checkbox" name="tech" value="py"> Python</label>
    <label><input type="checkbox" name="tech" value="go"> Go</label>
    <label><input type="checkbox" name="tech" value="rs"> Rust</label></fieldset>
</div></body></html>"""

# Visually-hidden "styled" radios, and a custom combobox that builds its options only when you type.
STYLED_CONTROLS = """<!doctype html><html><head><style>
.sr input{position:absolute;opacity:0;width:0;height:0}
.sr label{display:inline-block;padding:4px 10px;border:1px solid #999;cursor:pointer}
ul[role=listbox]{border:1px solid #999;list-style:none;padding:0}
</style></head><body><form>
  <div role="radiogroup" aria-labelledby="g_age"><div id="g_age">Are you 18 years of age or older? *</div>
    <span class="sr"><input type="radio" id="a1" name="age" value="y"><label for="a1">Yes</label></span>
    <span class="sr"><input type="radio" id="a2" name="age" value="n"><label for="a2">No</label></span></div>
  <label for="cb">Country *</label>
  <input id="cb" role="combobox" aria-autocomplete="list" autocomplete="off" type="text">
  <div id="host"></div>
</form>
<script>
const names = ["Mexico", "United States", "Canada", "Mexico City"];
const cb = document.getElementById('cb'), host = document.getElementById('host');
cb.addEventListener('input', () => {
  host.innerHTML = '';
  const q = cb.value.toLowerCase();
  if (!q) return;
  const ul = document.createElement('ul'); ul.setAttribute('role', 'listbox');
  names.filter(n => n.toLowerCase().includes(q)).forEach(n => {
    const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = n;
    li.addEventListener('click', () => { cb.value = n; cb.dataset.chosen = n; host.innerHTML = ''; });
    ul.appendChild(li);
  });
  host.appendChild(ul);
});
</script></body></html>"""
