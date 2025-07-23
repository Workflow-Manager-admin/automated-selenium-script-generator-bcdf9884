from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from bs4 import BeautifulSoup

app = FastAPI(
    title="Automated Selenium Script Generator Backend API",
    description="FastAPI backend that accepts HTML and test steps, parses the HTML, and finds relevant elements referenced by the test steps for Selenium script generation.",
    version="0.2.0",
    openapi_tags=[
        {"name": "Health", "description": "Healthcheck endpoints"},
        {"name": "HTML Test Parsing", "description": "Endpoints for parsing HTML and identifying relevant elements for test steps"},
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
        (r"\bwith\s+id\s+['\"]?([\w\-:]+)['\"]?", "id"),
        (r"\bwith\s+name\s+['\"]?([\w\-:]+)['\"]?", "name"),
        (r"\bwith\s+class\s+['\"]?([\w\-: ]+)['\"]?", "class"),
        (r"\belement\s+with\s+class\s+['\"]?([\w\-: ]+)['\"]?", "class"),
        (r"\btext\s*['\"]([^'\"]+)['\"]", "text"),
        (r"\bwith\s+text\s+['\"]([^'\"]+)['\"]", "text"),
        (r"\bwhere\s+text\s+is\s+['\"]([^'\"]+)['\"]", "text"),
        (r"\bxpath\s+['\"]([^'\"]+)['\"]", "xpath"),
    ]
    for pat, key in patterns:
        m = re.search(pat, step, re.IGNORECASE)
        if m:
            return {key: m.group(1).strip()}
    # Attempt: 'the <button/input/element> "<value>"'
    m = re.search(r"(button|input|element)\s+[\"']?([\w\-\s]+)[\"']?", step, re.IGNORECASE)
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
@app.post("/parse_html_test_steps",
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
