from fastapi import FastAPI, Body, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from bs4 import BeautifulSoup
import os
import httpx

app = FastAPI(
    title="Automated Selenium Script Generator Backend API",
    description="FastAPI backend that accepts HTML and test steps, parses the HTML, and finds relevant elements referenced by the test steps for Selenium script generation.",
    version="0.2.0",
    openapi_tags=[
        {"name": "Health", "description": "Healthcheck endpoints"},
        {"name": "HTML Test Parsing", "description": "Endpoints for parsing HTML and identifying relevant elements for test steps"},
        {"name": "Selenium Generation", "description": "Endpoints for generating Selenium Python scripts with Gemini LLM"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", tags=["Health"])
def health_check():
    """
    Health check endpoint.
    Returns a simple JSON message indicating that the service is running.
    """
    return {"message": "Healthy"}

# Request and response models

class TestStep(BaseModel):
    description: str = Field(..., description="Description of the test step, such as 'Click the button with id submitBtn'.")

class HTMLTestParseRequest(BaseModel):
    html: str = Field(..., description="The HTML source to be parsed.")
    test_steps: List[TestStep] = Field(..., description="A list of test step descriptions referencing HTML elements.")

class ElementMatch(BaseModel):
    step_index: int = Field(..., description="Index of the corresponding test step.")
    step_description: str = Field(..., description="Original test step description.")
    matched_element: Dict[str, Any] = Field(..., description="Dictionary of info about the matched HTML element.")
    match_method: str = Field(..., description="How the element was identified (id, class, name, text, etc.)")

class HTMLTestParseResponse(BaseModel):
    elements: List[ElementMatch] = Field(..., description="List of matched HTML elements per test step.")

# PUBLIC_INTERFACE
class GenerateSeleniumRequest(BaseModel):
    html: str = Field(..., description="Full HTML of web page.")
    test_steps: List[str] = Field(..., description="List of user-specified test steps (plain English).")

class GenerateSeleniumResponse(BaseModel):
    selenium_script: str = Field(..., description="Python code for Selenium (generated).")
    locators: List[Dict[str, Any]] = Field(..., description="Locator info extracted for steps.")

def _extract_element_reference(step: str) -> Dict[str, str]:
    """
    Attempt to parse the step text and extract likely reference info.
    Returns a dict with one of ["id", "name", "class", "text", "xpath"] set to the value referenced.
    Supports common English descriptions like:
      - "Click the button with id submitBtn"
      - "Type 'foo' in the input with name searchBox"
      - "Verify the text 'Welcome' is visible"
      - "Click on the element with class btn-primary"
    """
    import re
    # Pattern: with id/class/name <value>
    patterns = [
        (r"\bwith\s+id\s+[\'\"]?([\w\-:]+)[\'\"]?", "id"),
        (r"\bwith\s+name\s+[\'\"]?([\w\-:]+)[\'\"]?", "name"),
        (r"\bwith\s+class\s+[\'\"]?([\w\-: ]+)[\'\"]?", "class"),
        (r"\belement\s+with\s+class\s+[\'\"]?([\w\-: ]+)[\'\"]?", "class"),
        (r"\btext\s*[\'\"]([^\'\"]+)[\'\"]", "text"),
        (r"\bwith\s+text\s+[\'\"]([^\'\"]+)[\'\"]", "text"),
        (r"\bwhere\s+text\s+is\s+[\'\"]([^\'\"]+)[\'\"]", "text"),
        (r"\bxpath\s+[\'\"]([^\'\"]+)[\'\"]", "xpath"),
    ]
    for pat, key in patterns:
        m = re.search(pat, step, re.IGNORECASE)
        if m:
            return {key: m.group(1).strip()}
    # Attempt: 'the <button/input/element> "<value>"'
    m = re.search(r"(button|input|element)\s+[\"\']?([\w\-\s]+)[\"\']?", step, re.IGNORECASE)
    if m:
        return {"type": m.group(1).strip(), "text": m.group(2).strip()}
    return {}

def _find_element(soup: BeautifulSoup, ref: Dict[str, str]):
    """
    Given a soup and a reference dict, try to find the most relevant element(s).
    """
    if "id" in ref:
        el = soup.find(attrs={'id': ref["id"]})
        if el:
            return {"method": "id", "element": el}
    if "name" in ref:
        el = soup.find(attrs={'name': ref["name"]})
        if el:
            return {"method": "name", "element": el}
    if "class" in ref:
        # Classes may be space-separated; try all options
        class_vals = ref["class"].split()
        el = soup.find(attrs={'class': class_vals})
        if el:
            return {"method": "class", "element": el}
    if "text" in ref:
        # For visible text, try exact, then substring
        el = soup.find(lambda tag: tag.get_text(strip=True) == ref["text"])
        if el:
            return {"method": "text_exact", "element": el}
        el = soup.find(lambda tag: ref["text"] in tag.get_text(strip=True))
        if el:
            return {"method": "text_contains", "element": el}
    if "xpath" in ref:
        # Not implemented: XPath navigation; could use lxml, but skip for now
        return None
    return None

def _element_to_dict(element) -> Dict[str, Any]:
    """
    Converts a BeautifulSoup element to serializable info.
    """
    if not element:
        return {}
    return {
        "tag": element.name,
        "attributes": dict(element.attrs),
        "text": element.get_text(strip=True)
    }

# PUBLIC_INTERFACE
@app.post(
    "/parse_html_test_steps",
    response_model=HTMLTestParseResponse,
    summary="Parse HTML and identify elements needed for test steps",
    description="Accepts HTML and test step descriptions, parses the HTML, and extracts elements required for each test step. Uses NLP heuristics on step fields to guess IDs, names, classes, or text.",
    tags=["HTML Test Parsing"],
    responses={
        200: {
            "description": "Matched elements for each test step",
            "content": {"application/json": {"example": {
                "elements": [
                    {
                        "step_index": 0,
                        "step_description": "Click the button with id submitBtn",
                        "matched_element": {
                            "tag": "button",
                            "attributes": {"id": "submitBtn", "class": "primary"},
                            "text": "Submit"
                        },
                        "match_method": "id"
                    }
                ]
            }}}
        }
    }
)
def parse_html_test_steps(payload: HTMLTestParseRequest = Body(...)):
    """
    Receives HTML source and test step descriptions, parses the HTML, and attempts to match each test step to the referenced HTML element.

    - **html**: HTML code to parse (string)
    - **test_steps**: List of test step objects, each with natural language descriptions

    Returns a list containing, for each test step:
    - The step index
    - The original description
    - Information about the matched HTML element (if found): tag, attributes, text
    - The criterion that matched (id/name/class/text)
    """
    soup = BeautifulSoup(payload.html, "html.parser")
    results = []
    for idx, step in enumerate(payload.test_steps):
        ref = _extract_element_reference(step.description)
        match_info = _find_element(soup, ref)
        matched_element = _element_to_dict(match_info["element"]) if match_info and match_info.get("element") else {}
        match_method = match_info["method"] if match_info else "not found"
        results.append(ElementMatch(
            step_index=idx,
            step_description=step.description,
            matched_element=matched_element,
            match_method=match_method
        ))
    return HTMLTestParseResponse(elements=results)

# ================== NEW ENDPOINT BELOW =======================

# PUBLIC_INTERFACE
@app.post(
    "/generate_selenium_script",
    response_model=GenerateSeleniumResponse,
    summary="Generate Python Selenium script from HTML and test steps",
    description="""
Accepts HTML of a web page and test steps, analyzes the HTML to extract relevant locators, and generates Selenium Python code using Gemini LLM.
Authentication with Gemini is done via the GEMINI_API_KEY environment variable.

**Request Body:**
- html: str (HTML content)
- test_steps: List of step descriptions (str, English statements as automation steps)

**Returns:**
- selenium_script: Generated Python Selenium code (str)
- locators: List with locator info for each step/detected element

""",
    tags=["Selenium Generation"],
    responses={
        200: {
            "description": "Generated script and locator info.",
            "content": {"application/json": {"example": {
                "selenium_script": "# Selenium script ...\n",
                "locators": [
                    {
                        "step": "Click the button with id submitBtn",
                        "locator": "By.ID, 'submitBtn'",
                        "element": {"tag": "button", "attributes": {"id": "submitBtn"}}
                    }
                ]
            }}}
        }
    }
)
async def generate_selenium_script(payload: GenerateSeleniumRequest = Body(...)):
    """
    Receives HTML and test steps, parses HTML to find locators for each test step, then sends data to Gemini LLM to generate a Python Selenium script.

    - **html**: HTML code string
    - **test_steps**: List of steps (plain English description)

    Returns Selenium code string and a list of locator info for transparency.
    """
    soup = BeautifulSoup(payload.html, "html.parser")
    locators = []
    step_locator_details = []
    for idx, step in enumerate(payload.test_steps):
        ref = _extract_element_reference(step)
        match_info = _find_element(soup, ref)
        matched_element = match_info["element"] if match_info and match_info.get("element") else None
        match_method = match_info["method"] if match_info else "not found"
        elem_dict = _element_to_dict(matched_element)
        # Build locator string for Selenium (simple heuristics)
        locator_str = ""
        if match_method == "id" and elem_dict and "attributes" in elem_dict and "id" in elem_dict["attributes"]:
            locator_str = f"By.ID, '{elem_dict['attributes']['id']}'"
        elif match_method == "name" and elem_dict and "attributes" in elem_dict and "name" in elem_dict["attributes"]:
            locator_str = f"By.NAME, '{elem_dict['attributes']['name']}'"
        elif match_method == "class" and elem_dict and "attributes" in elem_dict and "class" in elem_dict["attributes"]:
            if isinstance(elem_dict["attributes"]["class"], list):
                classes = " ".join(elem_dict["attributes"]["class"])
            else:
                classes = elem_dict["attributes"]["class"]
            locator_str = f"By.CLASS_NAME, '{classes}'"
        elif match_method.startswith("text") and elem_dict and "text" in elem_dict:
            locator_str = f"# find element with visible text '{elem_dict['text']}'"
        else:
            locator_str = "# fallback/no-match"
        locators.append({
            "step": step,
            "locator": locator_str,
            "element": elem_dict
        })
        step_locator_details.append((step, elem_dict, locator_str))

    # Prepare prompt for Gemini LLM
    locators_summary = "\n".join([
        f'- Step: "{step}" | Locator: {locator} | Element: {element}'
        for step, element, locator in step_locator_details
    ])
    prompt = f"""
You are an expert QA Automation Engineer. Given the following web page HTML and a list of test steps, use the HTML to identify the right locators for each step (ID, name, class, or text as appropriate), and then write Python code using Selenium and the By selectors to implement the test steps.

Webpage HTML:
-----------------
{payload.html}
-----------------

Test Steps:
-----------------
{chr(10).join(payload.test_steps)}
-----------------

Locators (suggested by backend):
-----------------
{locators_summary}
-----------------

- Write idiomatic Python code with correct Selenium setup (use webdriver.Chrome(); include necessary imports).
- For each test step, choose the best locator strategy by comparing the step and matching element info.
- Only generate code for the supplied steps.
- Use self-explanatory variable names.
- Do not include the HTML or test steps in the output, only the code.

Respond with only a fully valid Python file.
"""

    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        # Note for future deployment: Must set GEMINI_API_KEY in the environment
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY environment variable not set.")

    # Call Gemini API: currently modeled for Gemini Pro via Vertex API (adapt as needed)
    # Official endpoint may vary: see https://ai.google.dev/gemini-api/docs
    # We use a synchronous HTTPX call for simplicity
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {gemini_api_key}"
    }
    data = {
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ]
    }

    # Google Gemini API v1 (model name, endpoint may change in prod usage)
    GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(GEMINI_API_URL, headers=headers, params={"key": gemini_api_key}, json=data)
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Gemini API error: ({response.status_code}) {response.text}")
            res_data = response.json()
            # Parse content
            selenium_script = ""
            # The structure below is from Gemini API docs
            if "candidates" in res_data and len(res_data["candidates"]) > 0:
                # Multi-turn, prefer first candidate
                candidate = res_data["candidates"][0]
                if "content" in candidate and "parts" in candidate["content"] and len(candidate["content"]["parts"]) > 0:
                    text_content = candidate["content"]["parts"][0].get("text", "")
                    selenium_script = text_content.strip()
            elif "promptFeedback" in res_data:  # fallback/diagnostic
                selenium_script = "# Gemini LLM feedback: " + str(res_data["promptFeedback"])
            else:
                selenium_script = "# Gemini LLM did not return a valid completion."
    except Exception as ex:
        raise HTTPException(status_code=502, detail="Error communicating with Gemini LLM: " + str(ex))

    return GenerateSeleniumResponse(
        selenium_script=selenium_script,
        locators=locators
    )
