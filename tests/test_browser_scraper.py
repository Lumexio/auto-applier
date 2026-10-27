"""Unit tests for the universal browser-based job parser (LinkedIn, Handshake, JSON-LD)."""

from __future__ import annotations

from scraper import parse_jobs_from_html


def test_parse_json_ld_job_posting() -> None:
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "JobPosting",
          "title": "Senior Python Backend Engineer",
          "description": "Building high scale distributed services.",
          "identifier": {
            "@type": "PropertyValue",
            "name": "Stripe",
            "value": "stripe-9988"
          },
          "hiringOrganization": {
            "@type": "Organization",
            "name": "Stripe"
          },
          "jobLocation": {
            "@type": "Place",
            "address": {
              "addressLocality": "Remote, US"
            }
          },
          "url": "https://stripe.com/jobs/9988"
        }
        </script>
      </head>
      <body><h1>Careers</h1></body>
    </html>
    """
    jobs = parse_jobs_from_html(html, base_url="https://stripe.com/jobs")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == "stripe-9988"
    assert job["title"] == "Senior Python Backend Engineer"
    assert job["company"] == "Stripe"
    assert job["apply_url"] == "https://stripe.com/jobs/9988"
    assert job["source"] == "json_ld"


def test_parse_linkedin_job_cards() -> None:
    html = """
    <div class="jobs-search-results">
      <ul class="jobs-search__results-list">
        <li>
          <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/4123456789/?trackingId=abc">
            Senior AI Engineer
          </a>
        </li>
        <li>
          <a class="base-card__full-link" href="/jobs/view/4123456790/">
            Full Stack Developer
          </a>
        </li>
        <li>
          <a href="/jobs/search-results/?currentJobId=4473691580&amp;refId=abc">
            <span>Senior React Developer</span>
            <span>Tech Corp</span>
            <span>Easy Apply</span>
          </a>
        </li>
        <li>
          <a href="/jobs/">Jobs</a>
        </li>
        <li>
          <a href="/jobs/preferences/?viewType=SEEKING">Preferences</a>
        </li>
      </ul>
    </div>
    """
    jobs = parse_jobs_from_html(html, base_url="https://www.linkedin.com/jobs")
    assert len(jobs) == 3
    assert jobs[0]["id"] == "4123456789"
    assert jobs[0]["title"] == "Senior AI Engineer"
    assert jobs[0]["apply_url"] == "https://www.linkedin.com/jobs/view/4123456789"
    assert jobs[0]["source"] == "linkedin"
    assert jobs[1]["id"] == "4123456790"
    assert jobs[1]["title"] == "Full Stack Developer"
    assert jobs[1]["apply_url"] == "https://www.linkedin.com/jobs/view/4123456790"
    assert jobs[2]["id"] == "4473691580"
    assert jobs[2]["title"] == "Senior React Developer"
    assert jobs[2]["company"] == "Tech Corp"
    assert jobs[2]["apply_url"] == "https://www.linkedin.com/jobs/view/4473691580"


def test_parse_handshake_job_cards() -> None:
    html = """
    <div class="handshake-search-list">
      <a href="https://app.joinhandshake.com/jobs/8877665?query=python">
        Software Engineer - Early Career
      </a>
      <a href="/jobs/8877666">
        Data Scientist Intern
      </a>
    </div>
    """
    jobs = parse_jobs_from_html(html, base_url="https://app.joinhandshake.com/job-search")
    assert len(jobs) == 2
    assert jobs[0]["id"] == "8877665"
    assert jobs[0]["title"] == "Software Engineer - Early Career"
    assert jobs[0]["apply_url"] == "https://app.joinhandshake.com/jobs/8877665"
    assert jobs[0]["source"] == "handshake"
    assert jobs[1]["id"] == "8877666"
    assert jobs[1]["title"] == "Data Scientist Intern"
    assert jobs[1]["apply_url"] == "https://app.joinhandshake.com/jobs/8877666"


def test_parse_generic_career_links_fallback() -> None:
    html = """
    <nav>
      <a href="/about">About Us</a>
      <a href="/contact">Contact</a>
    </nav>
    <main>
      <a href="/careers/infrastructure-architect">Infrastructure Architect</a>
      <a href="/jobs/lead-designer">Lead Product Designer</a>
    </main>
    """
    jobs = parse_jobs_from_html(html, base_url="https://example.com/company")
    assert len(jobs) == 2
    titles = [j["title"] for j in jobs]
    assert "Infrastructure Architect" in titles
    assert "Lead Product Designer" in titles
    assert "About Us" not in titles
