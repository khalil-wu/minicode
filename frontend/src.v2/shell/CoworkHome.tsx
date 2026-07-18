import { BrandMark } from "../components/icons";
import { Composer } from "../composer/Composer";

export const CoworkHome = () => {
  return (
    <div className="workbench-home mc-main-surface">
      <div className="workbench-home-layout">
        <section className="workbench-home-main">
          <div className="workbench-empty-brand">
            <div className="workbench-empty-mark" aria-hidden="true">
              <BrandMark size={22} />
            </div>
            <h1 className="workbench-empty-title">What do you want to build?</h1>
          </div>

          <div className="workbench-home-composer">
            <Composer minimal />
          </div>
        </section>
      </div>
    </div>
  );
};
