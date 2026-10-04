import { Alert, Card, Col, Empty, Row } from "antd";
import RotationForm from "../rotation/RotationForm";
import RotationResult from "../rotation/RotationResult";
import { useRotation } from "../rotation/useRotation";
import { isListedEtf } from "../symbols";

const MUTED = "#8b98b5";

/** Momentum rotation across a universe, on a practice window and an unseen exam window. */
export default function RotationPage() {
  const { runs, error, starting, start } = useRotation();
  const universe = runs.practice?.config.symbols ?? runs.exam?.config.symbols ?? [];
  const stocks = universe.filter((s) => !isListedEtf(s));
  return (
    <Row gutter={[16, 16]}>
      <Col xs={24} md={7} lg={6}>
        <RotationForm onRun={start} starting={starting} />
      </Col>
      <Col xs={24} md={17} lg={18}>
        {error && <Alert type="error" showIcon closable message={error} style={{ marginBottom: 16 }} />}
        {stocks.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="Hindsight warning: this universe has individual stocks"
            description={`${stocks.slice(0, 8).join(", ")}${stocks.length > 8 ? "…" : ""} are among today's biggest companies: picking them now and testing past years
              selects yesterday's winners in hindsight (survivorship bias), which flatters both the rotation and holding them. Sector ETFs
              don't have that problem.`}
          />
        )}
        {runs.practice || runs.exam ? (
          <>
            <RotationResult title="Practice" run={runs.practice} />
            <RotationResult title="Exam" run={runs.exam} />
          </>
        ) : (
          <Card>
            <Empty
              description={
                <span style={{ color: MUTED }}>
                  Momentum is one of the best-documented effects in markets: what rose over the past year tends to keep rising for a
                  while. Test it on the 11 sector ETFs first: the practice years, then the exam years it never saw.
                </span>
              }
            />
          </Card>
        )}
      </Col>
    </Row>
  );
}
